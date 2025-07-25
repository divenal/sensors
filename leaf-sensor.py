#!/usr/bin/env python3

import datetime
import gzip
import logging
import time
import sys

import pycarwings2
from sensors import Sensors

_logger = logging.getLogger(__name__)

"""Update the Leaf SoC as appropriate."""

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

def update_now(leaf, sensors):
    answer = leaf.get_latest_battery_status()
    _logger.debug(answer)
    record = answer["BatteryStatusRecords"]
    status = record["BatteryStatus"]
            
    # there seem to be 3 different date/times - are they all the same ?
    when = record["OperationDateAndTime"]
    soc = int(status["SOC"]["Value"])

    update = datetime.datetime.strptime(when, "%d-%b-%Y %H:%M")

    ts = update.timestamp()
    sensors.store(sensors.Leaf(int(ts), soc, int(time.time())))
    return (update, soc)
    
def monitor():

    sensors = Sensors()
    leaf = pycarwings2.getleaf()

    # update when we have charged, and plug-in state changes to 0, or no further charging planned
    status = 1   # zappi.status
    pending = 0  # iog.pending
    need_update = False
    updated_since_plugin = False

    while True:

        zappi = sensors.load(sensors.Zappi)
        iog = sensors.load(sensors.IOG)

        if need_update:
            try:
                (update, soc) = update_now(leaf, sensors)
                _logger.info("%s: %d%%", update, soc)

                need_update = False
                updated_since_plugin = True

            except pycarwings2.CarwingsError:
                _logger.warn("Got a CarwingsError")
            except:
                _logger.warn("Got some other error")

        # look to see if we want to update.
        # The update doesn't happen until next time round

        if zappi.status == 2:
            # need an update when we stop charging
            updated_since_plugin = False

        if zappi.status == 0 and status > 0 and not updated_since_plugin:
            # we've unplugged, and haven't updated for a while
            need_update = True

        if zappi.status > 0 and status == 0:
            # just plugged in
            updated_since_plugin = False

        if iog.pending == 0 and pending > 0:
            # have finished charging
            need_update = True

        status = zappi.status
        pending = iog.pending

        time.sleep(300)



if __name__ == "__main__":

    if len(sys.argv) == 1:
        # enter monitoring mode
        logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
        now = datetime.datetime.now()
        tstamp = now.strftime("%Y%m%d-%H%M")

        with gzip.open(filename="/tmp/leaf." + tstamp + ".log.gz", mode="wt") as zf:
            setup_logging(zf)
            monitor()
            
    elif sys.argv[1] == "now":
        (update, soc) = update_now(pycarwings2.getleaf(), Sensors())
        print(update, soc)

    else:
        print(f"usage: {sys.argv[0]} [now]")
