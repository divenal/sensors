#!/usr/bin/env python3

"""A simple script to print some state every 10 minutes.

The temperatures come from the daikin site.
The power consumption comes from the zappi sensor
"""

from datetime import datetime
import time
import logging
import gzip

from daikin import Daikin
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


def monitor():
    daikin = Daikin()
    sensors = Sensors()
    errors = 0

    while True:

        mp = daikin.management_points()
        now = datetime.now()

        # from time to time this produces empty results
        try:
            sd = mp["climateControlMainZone"]["sensoryData"]["value"]
            lwt = sd["leavingWaterTemperature"]["value"]
            outdoor = sd["outdoorTemperature"]["value"]
            room = sd["roomTemperature"]["value"]

            tc = mp["climateControlMainZone"]["temperatureControl"]["value"]
            # should these be "auto", or "heating" ?
            offs = tc["operationModes"]["auto"]["setpoints"]["leavingWaterOffset"]["value"]

            # room target is only available if we are using Madoka control
            sprt = tc["operationModes"]["auto"]["setpoints"].get("roomTemperature")
            target = sprt["value"] if sprt is not None else 20

            hwt = mp["domesticHotWaterTank"]["sensoryData"]["value"]
            hw = hwt["tankTemperature"]["value"]

            zappi = sensors.load(sensors.Zappi)

            # zappi sensor should update every few minutes.
            # if it's more than 10 minutes old, something is wrong

            z_age = now.timestamp() - zappi.timestamp
            if z_age > 30*60:
                _logger.warning("zappi power data is %d seconds old", z_age)
                power = -1
            else:
                power = zappi.hp

            s = sensors.Daikin(int(now.timestamp()),
                               outdoor,
                               Sensors.encode_rt(room),
                               Sensors.encode_rt(target),
                               hw,
                               lwt,
                               offs)

            sensors.store(s)

            _logger.info(
                "power=%4d outdoor=%2d room=%2.1f / %2.1f hw=%d  lwt=%d / %.1f (offs=%d)",
                power,
                outdoor,
                room,
                target,
                hw,
                lwt,
                s.targetlwt,
                offs,
            )

            errors = 0
        except KeyError as ke:
            _logger.warning("Hmm - got a key error %s", ke)
            errors = errors + 1

        # Daikin API requests are limited to 200 per day
        # They suggest one per 10 minutes, which leaves around 50 for
        # actually controlling the system. Or perhaps downloading
        # consumption figures at the end of the day.
        # Let's do 10 minutes during the day, 15 minutes overnight,
        # but 1 minute after one error. But only once - if errors
        # persist, we'll just use up all our requests
        time.sleep(60 if errors == 1
                   else 600 if 6 <= now.hour <= 21
                   else 900)


def main():
    now = datetime.now()
    tstamp = now.strftime("%Y%m%d-%H%M")

    with gzip.open(filename="/tmp/daikin." + tstamp + ".log.gz", mode="wt") as zf:
        setup_logging(zf)
        monitor()


if __name__ == "__main__":
    main()
