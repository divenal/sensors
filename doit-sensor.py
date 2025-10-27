from datetime import datetime
from signal import alarm
from math import isnan
import time
import logging
import gzip
import aioesphomeapi
import asyncio
import sys
from sensors import Sensors

_logger = logging.getLogger(__name__)

# The doit esp32 is monitoring two (sets of) sensors via esphome:
#   dallas thermometers in airing cupboard
#   xiaomi humidity thingy
# We use aioesphomeapi to connect as a client to receive updates to these sensors.

# We'll make a singleton of this, to store stuff we need

class doit_class:
    def __init__(self):
        # map from sensor key to sensor id, since changes are reported only with key
        self.sensors = dict()

        # readings arrive one at a time, so we need to collate them
        # seems to use the names, with space => _ rather than the id
        self.values = {
            "flow_from_hp": 20.0, "return_from_rads": 20.0, "after_valve": 20.0,"return_to_hp": 20.0,
            "pvvx_temperature": 20.0, "pvvx_humidity": 50.0, "pvvx_battery-voltage": 2.5, "pvvx_battery-level": 40.0
            }

        # use set to recall which readings have been added since the last publish
        self.flows = set()
        self.pvvx = set()

doit = doit_class()

sensors = Sensors()

def record_flow():
    now = int(time.time())
    zappi = sensors.load(sensors.Zappi)
    daikin = sensors.load(sensors.Daikin)

    # zappi sensor should update every few minutes.
    # if it's more than 10 minutes old, something is wrong

    z_age = now - zappi.timestamp
    if z_age > 30*60:
        _logger.warning("zappi power data is %d seconds old", z_age)
        power = -1
    else:
        power = zappi.hp

    t = doit.values
    flow = t['flow_from_hp']
    back = t['return_from_rads']
    after = t['after_valve']
    out = t['return_to_hp']
    _logger.info(
                "power=%4d flow=%.2f back=%.2f after=%.2f out=%.2f target=%d (offs %d)",
                power,
                flow,
                back,
                after,
                out,
                daikin.targetlwt,
                daikin.offset)

    s = Sensors.Doit(now,
                     Sensors.Doit.encode(flow),
                     Sensors.Doit.encode(back),
                     Sensors.Doit.encode(after),
                     Sensors.Doit.encode(out))
    sensors.store(s)

def record_xiaomi():
    now = int(time.time())

    t = doit.values
    temp = t['pvvx_temperature']
    humid = t['pvvx_humidity']
    voltage = t['pvvx_battery-voltage']
    battery = t['pvvx_battery-level']
    _logger.info(
                "xiaomi temp=%.2f humidity=%.2f voltage=%.2f battery=%d%%",
                temp,
                humid,
                voltage,
                battery)

    s = Sensors.Xiaomi(now,
                       0,  # cycle not available ?
                       Sensors.encode_rt(temp),
                       int(humid),
                       int(battery))
    sensors.store(s)

# This is invoked for each sensor state change reported by the device
def change_callback(state):
    """Record state changes reported by the device.."""
    alarm(600)    # reset watchdog
    id = doit.sensors[state.key]
    val = state.state
    if state.missing_state:
        _logger.warning("missing state for %s", id)
    elif isnan(val):
        _logger.warning("Nan for %s", id)
    else:
        doit.values[id] = val
        if id.startswith("pvvx"):
            doit.pvvx.add(id)
            if len(doit.pvvx) == 4:
                # we have the full set
                record_xiaomi()
                doit.pvvx.clear()
        else:
            doit.flows.add(id)
            if len(doit.flows) == 4:
                # we have the full set
                record_flow()
                doit.flows.clear()

async def main(dest):
    """Connect to an ESPHome device and get details."""

    # Establish connection
    api = aioesphomeapi.APIClient(dest, 6053, "MyPassword")
    await api.connect(login=True)

    # List all entities of the device
    entities = await api.list_entities_services()
    # entities is a list of arrays, or something ?  Only need the first one
    for e in entities[0]:
        # print(f"key {e.key} = id {e.object_id}")
        doit.sensors[e.key] = e.object_id

    # Now we can subscribe to the state changes (spins off a task)
    api.subscribe_states(change_callback)


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

if __name__ == "__main__":

    if len(sys.argv) < 2:
        print("usage: test2.py dest")
        sys.exit()

    alarm(600)    # watchdog
    now = datetime.now()
    tstamp = now.strftime("%Y%m%d-%H%M")
    with gzip.open(filename="/tmp/doit." + tstamp + ".log.gz", mode="wt") as zf:
        setup_logging(zf)
        loop = asyncio.get_event_loop()
        try:
            asyncio.ensure_future(main(sys.argv[1]))
            loop.run_forever()
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()

