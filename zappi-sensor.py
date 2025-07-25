#!/usr/bin/env python3

import datetime
import gzip
import logging
import time

from myenergi import MyenergiApi
from sensors import Sensors

_logger = logging.getLogger(__name__)

"""Maintain the zappi state in the Sensors memory-map."""

def setup_logging(zf):
    # log to both zf and console

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

def monitor():
    myenergi = MyenergiApi()
    sensors = Sensors()

    # map from zappi status code to our status field
    statusmap = { "A": 0, "B1": 1, "B2": 1, "C1" : 1, "C2": 2, "F": -1 }

    last_update = 0
    while True:
        stat = myenergi.get('/cgi-jstatus-Z')
        zappi = stat['zappi'][0]

        dmy = zappi["dat"]  # "dd-mm-yyyy"
        hms = zappi["tim"]  # "HH:MM:SS"

        # zappi always runs in utc
        update = datetime.datetime(day = int(dmy[0:2]),
                                   month = int(dmy[3:5]),
                                   year = int(dmy[6:]),
                                   hour = int(hms[0:2]),
                                   minute = int(hms[3:5]),
                                   second = int(hms[6:8]),
                                   tzinfo = datetime.timezone.utc)

        now = update.timestamp()
        if now > last_update:
            last_update = now
            zappi = sensors.Zappi(timestamp = int(update.timestamp()),
                                  mode = zappi["zmo"],
                                  status = statusmap[zappi["pst"]],
                                  car = zappi["ectp1"],
                                  grid = zappi["ectp2"],
                                  hp = -zappi["ectp3"],
                                  lock = zappi["lck"])
            _logger.info(zappi)
            sensors.store(zappi)
        else:
            _logger.warning("no change since last iteration")

        time.sleep(120)


if __name__ == "__main__":
    now = datetime.datetime.now()
    tstamp = now.strftime("%Y%m%d-%H%M")

    with gzip.open(filename="/tmp/zappi." + tstamp + ".log.gz", mode="wt") as zf:
        setup_logging(zf)
        monitor()
