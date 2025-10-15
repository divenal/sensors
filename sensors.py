#!/usr/bin/env python3

# A scheme for sharing sensor values between scripts, based
# on a memory-mapped file. This file basically defines the
# sensor memory layouts
#
# Byte offsets - generally in chunks of 32 bytes
#    0   zappi
#   32   givenergy
#   64   Intelligent Octopus Go schedule
#   96   xaiomi bluetooth thermometer
#  128   daikin heatpump
#  160   /extra filesystem usage
#  192   leaf battery
#  224   doit dallas thermometers
# generally first 4 bytes for each sensor are unix timestamp of last update

import datetime
import mmap
import os
import struct
from collections import namedtuple

class Sensors:

    """A class to manage sensors which store their state in a mmap.

    A sensor is implemented as a namedtuple.
    """
    
    # Named tuples gives class-like access, but can also
    # be easily packed and unpacked to/from byte array
    # With each type, we associate a packing format and
    # a file offset.

    # NOTE: This does assume a single instance of each sensor type...
    # might have to rethink that one one day....

    # TODO: publish the data / poke the dashboard process in some way.
    # One option is to use a MQTT-SN server, using sensor offset as
    # the predefined topic number. SN means I can use udp, which means
    # the sensor process doesn't have to worry about maintaining a tcp
    # connection, and worrying about retrying, etc.
    # Then dashboard can subscribe to updates.
    # But don't really need full MQTT - maybe the dashboard process
    # can just listen on udp directly. The MQTT-SN publish format
    # would do as the message format.  (In effect, dashboard is
    # acting as a very stripped-down MQTT-SN server.)

    ##############

    # I don't like communicating using floating point, so where possible
    # values are encoded as integer in some way. Eg room temperature are
    # signed offset from 20.0 in steps of 0.1
    @staticmethod
    def encode_rt(rt):
        return int((rt - 20.0) * 10.0)

    ############################

    # The Sensors:

    # A Zappi sensor: stores
    #   mode (1=fast, 2=eco, 3=eco+, 4=stopped)
    #   car status (-1 = error, 0 = unplugged, 1 = plugged, 2 = charging)
    #   three ct-clamp readings - car, grid and heatpump (ct3)

    Zappi = namedtuple('Zappi', 'timestamp, mode, status, car, grid, hp, lock')
    Zappi.fmt = '!Lbbhhhb'
    Zappi.offs = 0

    ###############

    # A givenergy sensor: stores (power in watts)
    #   solar (instant and MA)
    #   grid import (instant and MA : -ve for importing)
    #   battery discharge (instant and MA : -ve for charging) 
    #   SoC
    #   AC generation (instanct and MA)
    #   EPS (to detect outage)

    GivEnergy = namedtuple("GivEnergy", 'timestamp, solar, solarMA, grid, gridMA, battery, batteryMA, soc, ac, acMA, eps')
    GivEnergy.fmt = "!LhhhhhhBhhh"
    GivEnergy.offs = 32

    ###############

    # IOG sensor: stores
    #  total pending slots (raw from Octopus)
    #  count of pending sanitised slots (only those outside standard cheap rate)
    #  up to 3 start/end pairs, expressed in 30-minute intervals (3 = 0130, etc)
    #  (defaults apply to the trailing fields)
    IOG = namedtuple("IOG",
                     "timestamp, pending, count, s1, e1, s2, e2, s3, e3",
                     defaults = (0, 0, 0, 0, 0, 0)
                    )
    IOG.fmt = "!LBB6B"
    IOG.offs = 64

    ###############

    # Xaomi bluetooth thermometer
    # temperature and humidity available to 2dp, but since they're
    # not calibrated, not terribly meaningful
    # Store temp as signed delta from 20 degrees, in tenths.
    # ie 3 = 20.3, -4 = 19.6

    # Use a subclass so we can use a property, and a create fn that will
    # do the encoding

    class Xaomi(namedtuple("Xaiomi_",
                           "timestamp, cycle, rt_, humidity, battery")):
        fmt = "!LBbBB"
        offs= 96

        @property
        def rt(self):
            return 20 + 0.1 * self.rt_

    ###############

    # Daikin heatpump

    class Daikin(namedtuple("Daikin_",
                           "timestamp, outdoor, room_, target_, hw, lwt, offset")):
        scale = 10
        fmt = "!LbbbBBb"
        offs= 128

        @property
        def room(self):
            return 20 + 0.1 * self.room_

        @property
        def target(self):
            return 20 + 0.1 * self.target_

        # weather curve
        X1 = -2
        Y1 = 45
        X2 = 15
        Y2 = 28

        @property
        def targetlwt(self):
            tod = min(max(self.outdoor, self.X1), self.X2)
            return self.Y1 + (tod - self.X1)/(self.X2 - self.X1) * (self.Y2-self.Y1) + self.offset

    ###############

    # Leaf - stores the SoC
    # Has two timestamps - when the server was last updated, and
    # when we last checked the server.

    Leaf = namedtuple("Leaf", 'timestamp, soc, checked')
    Leaf.fmt = "!LBL"
    Leaf.offs = 192

    ###############

    # Octopus Greener Days forecast
    # Stores timestamp of first day, then 7 scores

    GreenerDays = namedtuple("GreenerDays",
                             "timestamp, a, b, c, d, e, f, g")
    GreenerDays.offs = 224
    GreenerDays.fmt = "!L7B"

    ###############

    # doit sensor for dallas thermometers
    # Readings seem to go up in steps of 0.0625 (1/16 of a degree),
    # so multiply by 16 and store as a 16-bit int
    #
    class Doit(namedtuple("Doit_",
                           "timestamp, flow_, back_, after_, out_")):
        fmt = "!Lhhhh"
        offs= 224

        def encode(temp):
            return int(temp * 16 + 0.5)

        @property
        def flow(self):
            return 0.0625 * self.flow_

        @property
        def back(self):
            return 0.0625 * self.back_

        @property
        def after(self):
            return 0.0625 * self.after_

        @property
        def out(self):
            return 0.0625 * self.out_

    #############################

    # The sensor class proper

    def __init__(self, mode="rw"):
        fd = os.open("/tmp/sensors",
                     (os.O_RDWR | os.O_CREAT if mode=="rw" else os.O_RDONLY) | os.O_CLOEXEC,
                      mode=0o664)

        try:
            if mode=="rw": os.truncate(fd, 1024)  # ensure at least 1024 bytes
            self.map = mmap.mmap(fd, 1024, prot=(mmap.PROT_READ | mmap.PROT_WRITE if mode=="rw" else mmap.PROT_READ))
        finally:
            os.close(fd)

    def store(self, tuple):
        """Write the sensor instance into the appropriate place in the map."""
        struct.pack_into(tuple.fmt, self.map, tuple.offs, *tuple)

    def load(self, Tuple):
        """Create an instance of the given sensor (tuple) type from the map."""
        return Tuple._make(struct.unpack_from(Tuple.fmt, self.map, offset=Tuple.offs))

# if invoked as a script, just instantiate and display each known sensor

if __name__ == "__main__":
    s = Sensors(mode="r")

    # sensors include an update timestamp, so show age of result
    now = int(datetime.datetime.now().timestamp())
    for T in (Sensors.Zappi, Sensors.GivEnergy, Sensors.IOG, Sensors.Xaomi, Sensors.Leaf, Sensors.Daikin, Sensors.GreenerDays, Sensors.Doit):
        t = s.load(T)
        age = now - t.timestamp
        if age > 7200:
            age = f"{age / 3600.0:.2f} hours"
        elif age > 120:
            age = f"{age / 60.0:.2f} mins"
        else:
            age = str(age) + " seconds"
        
        print(t, " -- ", age)
    d = s.load(Sensors.Daikin)
    print(f"daikin room: {d.room:.1f}  targetlwt: {d.targetlwt:.1f}")

