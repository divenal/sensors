#!/bin/env python3

# Zappi seems to multicast raw ethernet packets with all sorts of binary data.
# I want to access CT clamp readings without having to go out to the cloud to get them,
# so I've been trying to spot the data I need inside these frames.
#
# It requires superpowers to open a raw socket, and I don't want to grant that to
# python. So a separate wrapper 'opensock' does that, and passes on the opened fd as #42
# See opensock.c

import gzip
import logging
import struct
import socket
import time

from datetime import datetime
from signal import alarm

from sensors import Sensors

_logger = logging.getLogger(__name__)

def setup_logging(zf):
    # log to both file and console

    gz_log_handler = logging.StreamHandler(zf)
    _logger.addHandler(gz_log_handler)

    stderr_log_handler = logging.StreamHandler()
    _logger.addHandler(stderr_log_handler)

    # prefix timestamp onto the file logger
    formatter = logging.Formatter(
        fmt="%(asctime)s: %(message)s", datefmt="%Y-%m-%d--%H:%M"
    )
    gz_log_handler.setFormatter(formatter)

    _logger.setLevel(logging.DEBUG)

def main():
    sock = socket.fromfd(42, socket.AF_PACKET, socket.SOCK_RAW)
    sensors = Sensors()

    car = 0
    grid = 0
    hp = 0
    alarm(600)

    while True:
        frame = sock.recv(2000)
        # First 14 bytes are mac address and protocol
        # Then the first 26 bytes of payload are a mostly fixed header
        #   CB DA E9 F8, then 16*00, then 0x01, then three more zeros.
        # Then the length. Next byte after that always seems to be 0xe5
        # Everything after that we regard as the payload.
        #

        if len(frame) == 14 + 26 + 33:
            # Every second, contains grid and car power
            grid, car = struct.unpack("<40x 4x i 15x h 8x", frame)
        elif len(frame) == 14 + 26 + 49:
            # Every 12 seconds or so, also has HP power
            # But the car power is very confusing, reporting
            # 7kW when not charging, so just ignore it
            xcar, grid, hp = struct.unpack("<40x 12x h 12x h 6x h 13x", frame)
            alarm(120)
            sensors.store(Sensors.Snoop(int(time.time()), grid, car, -hp))
        elif len(frame) == 14 + 26 + 60:
            # this arrives about every 60 seconds - use this to trigger logging
            _logger.info("grid %4d, car %4d, hp %4d", grid, car, -hp)


if __name__ == "__main__":
    now = datetime.now()
    tstamp = now.strftime("%Y%m%d-%H%M")

    with gzip.open(filename="/tmp/snoop." + tstamp + ".log.gz", mode="wt") as zf:
        setup_logging(zf)
        main()
