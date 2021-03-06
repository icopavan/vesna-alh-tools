import binascii
import itertools
import logging
import re
import struct
import time

from vesna.spectrumsensor import Device, DeviceConfig, ConfigList, SweepConfig, Sweep
from vesna.alh import CRCError

log = logging.getLogger(__name__)

class SpectrumSensorProgram:
	"""Describes a single spectrum sensing task."""

	def __init__(self, sweep_config, time_start, time_duration, slot_id):
		"""Create a new spectrum sensing task.

		sweep_config -- Frequency sweep configuration to use.
		time_start -- Time to start the task (UNIX timestamp).
		time_duration -- Duration of the task in seconds.
		slot_id -- Numerical slot id used for storing measurements.
		"""
		self.sweep_config = sweep_config
		self.time_start = time_start
		self.time_duration = time_duration
		self.slot_id = slot_id

class SpectrumSensorResult:
	"""Result of a spectrum sensing task."""

	def __init__(self, program):
		"""Create a new result object.

		program -- SpectrumSensorProgram object that produced these results.
		"""
		self.program = program
		self.sweeps = []

	def get_hz_list(self):
		"""Return a list of frequencies in hertz covered by this result.
		"""
		return self.program.sweep_config.get_hz_list()

	def get_s_list(self):
		"""Return a list of timestamps in seconds covered by this result.
		"""
		return [ sweep.timestamp for sweep in self.sweeps ]

	def get_data(self):
		"""Return power measurements in dbm in form a two-dimensional array.
		"""
		data = []

		row_len = len(self.program.sweep_config.get_ch_list())

		for sweep in self.sweeps:
			if len(sweep.data) == row_len:
				row = sweep.data
			else:
				# only last row can be shorter
				assert len(data) == len(self.sweeps) - 1

				row = sweep.data + [sweep.data[-1]] * (row_len - len(sweep.data))

			data.append(row)

		return data

	def write(self, path):
		"""Write measurements into a tab-separated-values file.

		path -- path to the file to write
		"""

		outf = open(path, "w")

		outf.write("# t [s]\tf [Hz]\tP [dBm]\n")

		sweep_config = self.program.sweep_config
		num_channels = sweep_config.num_channels
		sweep_time = 0.0

		next_sweep_i = iter(self.sweeps)
		next_sweep_i.next()
		i = itertools.izip_longest(self.sweeps, next_sweep_i)

		for sweepnr, (sweep, next_sweep) in enumerate(i):
			assert isinstance(sweep, Sweep)

			if next_sweep is not None:
				sweep_time = next_sweep.timestamp - sweep.timestamp

			for dbmn, dbm in enumerate(sweep.data):

				time = sweep.timestamp + sweep_time/num_channels * dbmn

				channel = sweep_config.start_ch + sweep_config.step_ch * dbmn
				assert channel < sweep_config.stop_ch

				freq = sweep_config.config.ch_to_hz(channel)

				outf.write("%f\t%f\t%f\n" % (time, freq, dbm))

			outf.write("\n")

		outf.close()

class SpectrumSensor:
	"""ALH node acting as a spectrum sensor."""
	MAX_TIME_ERROR = 2.0

	def __init__(self, alh):
		"""Create a spectrum sensor based on an ALH implementation.

		alh -- ALH implementation used to communicate with the node
		"""
		self.alh = alh

	def sweep(self, sweep_config):
		"""Perform a single frequency sweep and return results
		immediately

		sweep_config -- Frequency sweep configuration to use.
		"""

		response = self.alh.post("sensing/quickSweepBin",
				"dev %d conf %d ch %d:%d:%d" % (
				sweep_config.config.device.id,
				sweep_config.config.id,
				sweep_config.start_ch,
				sweep_config.step_ch,
				sweep_config.stop_ch))

		data = response[:-4]
		crc = response[-4:]

		their_crc = struct.unpack("i", crc[-4:])[0]
		our_crc = binascii.crc32(data)
		if their_crc != our_crc:
			# Firmware versions 2.29 only calculate CRC on the
			# first half of the response due to a bug
			our_crc = binascii.crc32(data[:len(data)/2])
			if their_crc != our_crc:
				raise CRCError
			else:
				log.warning("working around broken CRC calculation! "
						"please upgrade node firmware")

		assert sweep_config.num_channels * 2 == len(data)

		sweep = Sweep()
		sweep.timestamp = 0
		for n in xrange(0, len(data), 2):
			datum = data[n:n+2]

			dbm = struct.unpack("h", datum)[0]*1e-2
			sweep.data.append(dbm)

		return sweep

	def program(self, program):
		"""Send the given spectrum sensing program to the node.

		program -- SpectrumSensorProgram object
		"""

		self.alh.post("sensing/freeUpDataSlot", "1", "id=%d" % (program.slot_id))

		time_before = time.time()

		relative_time = int(program.time_start - time_before)
		if relative_time < 0:
			raise Exception("Start time can't be in the past")

		self.alh.post("sensing/program",
			"in %d sec for %d sec with dev %d conf %d ch %d:%d:%d to slot %d" % (
				relative_time,
				program.time_duration,
				program.sweep_config.config.device.id,
				program.sweep_config.config.id,
				program.sweep_config.start_ch,
				program.sweep_config.step_ch,
				program.sweep_config.stop_ch,
				program.slot_id))

		time_after = time.time()

		time_error = time_after - time_before
		if time_error > self.MAX_TIME_ERROR:
			raise Exception("Programming time error %.1f s > %.1fs" % 
					(time_error, self.MAX_TIME_ERROR))

	def is_complete(self, program):
		"""Return true if given program has been successfuly completed.

		program -- SpectrumSensorProgram object
		"""
		if time.time() < program.time_start + program.time_duration:
			return False
		else:
			resp = self.alh.get("sensing/slotInformation", "id=%d" % (program.slot_id,))
			return "status=COMPLETE" in resp

	def _decode(self, program, data):
		num_channels = program.sweep_config.num_channels
		line_bytes = num_channels * 2 + 4

		result = SpectrumSensorResult(program)

		sweep = Sweep()
		for n in xrange(0, len(data), 2):
			datum = data[n:n+2]
			if len(datum) != 2:
				continue

			if n % line_bytes == 0:
				# got a time-stamp
				t = data[n:n+4]
				tt = struct.unpack("<I", t)[0]
				assert not sweep.data
				sweep.timestamp = tt * 1e-3
				continue

			if n % line_bytes == 2:
				# second part of a time-stamp, just ignore
				assert not sweep.data
				continue

			dbm = struct.unpack("h", datum)[0]*1e-2
			sweep.data.append(dbm)

			if len(sweep.data) >= num_channels:
				result.sweeps.append(sweep)
				sweep = Sweep()

		if(sweep.data):
			result.sweeps.append(sweep)

		return result

	def retrieve(self, program):
		"""Retrieve results from the given spectrum sensing program.

		Returns an SpectrumSensorResult object.

		program -- SpectrumSensorProgram object
		"""
		resp = self.alh.get("sensing/slotInformation", "id=%d" % (program.slot_id,))
		assert "status=COMPLETE" in resp

		g = re.search("size=([0-9]+)", resp)
		total_size = int(g.group(1))

		#print "total size:", total_size

		p = 0
		max_read_size = 512
		data = ""

		while p < total_size:
			chunk_size = min(max_read_size, total_size - p)

			#if p < total_size - chunk_size*2:
			#	p += max_read_size
			#	continue

			#print "start", p
			#print "size", chunk_size

			chunk_data_crc = self.alh.get("sensing/slotDataBinary", "id=%d&start=%d&size=%d" % (
				program.slot_id, p, chunk_size))

			chunk_data = chunk_data_crc[:-4]

			#print "len", len(chunk_data)
			
			their_crc = struct.unpack("i", chunk_data_crc[-4:])[0]
			our_crc = binascii.crc32(chunk_data)

			if(their_crc != our_crc):
				raise CRCError

			data += chunk_data

			p += max_read_size

		return self._decode(program, data)

	def get_config_list(self):
		"""Query and return the list of supported device configurations.

		Returns a ConfigList object.
		"""
		config_list = ConfigList()

		device = None
		config = None

		description = self.alh.get("sensing/deviceConfigList")
		for line in description.split("\n"):
			g = re.match("dev #([0-9]+), (.+), [0-9]+ configs:", line)
			if g:
				device = Device(int(g.group(1)), g.group(2))
				config_list._add_device(device)
				continue

			g = re.match("  cfg #([0-9]+): (.+):", line)
			if g:
				config = DeviceConfig(int(g.group(1)), g.group(2), device)
				config_list._add_config(config)
				continue

			g = re.match("     base: ([0-9]+) Hz, spacing: ([0-9]+) Hz, bw: ([0-9]+) Hz, channels: ([0-9]+), time: ([0-9]+) ms", line)
			if g:
				config.base = int(g.group(1))
				config.spacing = int(g.group(2))
				config.bw = int(g.group(3))
				config.num = int(g.group(4))
				config.time = int(g.group(5))
				continue

		return config_list
