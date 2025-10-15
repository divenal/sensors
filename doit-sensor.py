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

# We'll make a singleton of this, to store stuff we need

class doit_class:
    def __init__(self):
        # map from sensor key to sensor id, since changes are reported only with key
        self.sensors = dict()

        # readings arrive one at a time, so we need to collate them
        # seems to use the names, with space => _ rather than the id
        self.temps = { "flow_from_hp": 20.0, "return_from_rads": 20.0, "after_valve": 20.0,"return_to_hp": 20.0 }

        # use a set to recall which readings have been added since the last publish
        self.updated = set()

doit = doit_class()

sensors = Sensors()

def record():
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

    t = doit.temps
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
    
# This is invoked for each sensor state change reported by the device
def change_callback(state):
    """Print the state changes of the device.."""
    id = doit.sensors[state.key]
    val = state.state
    if state.missing_state:
        _logger.warning("missing state for %s", id)
    elif isnan(val):
        _logger.warning("Nan for %s", id)
    else:
        doit.temps[id] = val
        doit.updated.add(id)
        if len(doit.updated) == 4:
            # we have the full set
            record()
            doit.updated.clear()


async def main(dest):
    """Connect to an ESPHome device and get details."""

    # Establish connection
    api = aioesphomeapi.APIClient(dest, 6053, "MyPassword")
    await api.connect(login=True)

    # List all entities of the device
    entities = await api.list_entities_services()
    # entities is a list of arrays, or something ?  Only need the first one
    for e in entities[0]:
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

