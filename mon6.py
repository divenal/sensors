#!/usr/bin/env python3

# Version 6

"""
This script has various tasks:
  1) try to maintain Soc around the value configured
  2) monitor AC generation to avoid clipping if possible
  3) turn eco off between 2330 and about 0100
  4) export readings via my "sensors" package
  5) pause discharge during IOG charging periods conveyed via sensors.
     - *TODO* perhaps actively charge ?

In addition, there are some external api scripts doing more complex things
(to keep this as simple and focussed as possible.) Though more and more of those
tasks are migrating into this script.

Dynamic configuration is through two routes:
  - inverter registers
  - my 'sensors' mechanism, which is just a memory-mapped file with a block of bytes allocated
    to each sensor. This way, the script can respond to what other sensors are reporting.

Static configuration
- The pause-timer is used to define when this script is in charge ("daytime mode"). Outside of that timer,
  the script is (mostly) idle - it can still do tasks 3/4, though.
- Charge/discharge slots #3 through to #7 are used to control (solar) charging and export.
  The charge slot time windows are empty, and the charge limit sets the target SoC
  The discharge slot time windows are assumed to be contiguous, so the script cares about start
  of #3 to end of #7. The actual boundaries are ignored, and the script *assumes* that
  - #3 is active until 4pm
  - #4 is active until 7.30pm
  - #5 until 10pm
  - #6 till 11pm
  - #7 after 11pm (no hysteresis)
  Any discharge limit is implemented by the inverter as usual.
- Charging slots #3 .. #7 are used to set target SoC for (1)
  Those slots have empty time windows - it's just the target parameters we use.
   - if soc higher than X+5%, we force discharge (as long as we are within #3/#7 times)
   - if it's lower than X-5%, we allow solar charging, otherwise it's exported directly.
- Charging slot #2 is configured for overnight charge. We look at the limit and choose
  a suitable charging power to get us there.
- battery_{charge,discharge}_limit (111,112) control overall battery limits, including solar charging and eco
  mode, but more useful are the _ac versions (313,314) which control power just during ac charging or forced export.
  So the former are left on their max, and the latter stay at around 50%


*TODO* perhaps just get rid of this 'active' thing - just make the script active
all the time ?  Set pause timer to be something like 0100 to 0059 (so that the
missing minute happens while charging from grid)
Then we can use pause-discharge to idle the battery, rather than turning eco off.

For (2), it turns out that turning eco off provides the required
grid-first functionality - max out the AC generation, and send excess
to battery. It will even switch between charging and discharging
to keep AC maxed out, if a timed-export is enabled at the same time.

So normally, pause-mode = pause-charging, eco = on
But at high solar, pause-mode = not-paused, eco = off

Of course the downside is that turning off eco also disables
dynamic discharging, so want to avoid having eco off when solar
is low.


For (1) the discharge part is relatively straightforward - discharge timers #3..#7
are already reserved for this, and the battery_discharge_limit_ac is left at around 50%,
so just need to enable and disable dc discharge.

However, when we are forcing a discharge, we're not responding to any dynamic
demand. Which is a bit of a risk. I'm wondering if it should only do this as long
as solar is at least, say, 500W, so that combined power is >= 2kW. Or better to
monitor grid activity, and if it drops below export of 500W, go back to eco.


(3) is to allow the battery to idle before recharging, to maybe balance cells.
This was previously done using a discharge-timer with limit of 100%, but in
firmware #309 that seems to charge from grid at around 300W for some reason.
And charging timers with low target SoC still seem to charge to 20%.

2330 is start of IOG cheap rate. Could perhaps use start of AC charge time as
the end time. Rather than hard-coding 2330, could abuse some arbitrary timer,
such as AC charge slot #2, with both start and end = 23:30, so that it has no
effect on the inverter, but still gives us a parameter.
"""

import signal
import asyncio
from datetime import datetime
import gzip
import logging
import sys

from sensors import Sensors

from givenergy_modbus.client.client import Client
from givenergy_modbus.model.plant import Plant
from givenergy_modbus.model.register import HR, IR

_logger = logging.getLogger(__name__)

class MovingAverage:
    def __init__(self, fast=0.75, slow=0.1, decay=0.05):
        self.ff = fast
        self.sf = slow
        self.df = decay
        self.now = 0
        self.fast = None
        self.slow = None
        self.decay = 0

    @staticmethod
    def _merge(old, new, factor):
        return new if old is None else new * factor + old * (1-factor)

    def update(self, value):
        self.now = value
        self.fast = self._merge(self.fast, value, self.ff)
        self.slow = self._merge(self.slow, value, self.sf)
        self.decay = max(self.fast, self._merge(self.decay, self.fast, self.df))

# sub-class Plant in order to override register_written() to
# observe changes being made to the settings.
class MyPlant(Plant):

    # we are interested in when dp changes
    dpchanged = None

    # deprecated
    def registers_updated(self, reg, count, values):
        if count == 1:
            # This is *usually* because a register has changed.
            # But note that retrieving a value through the cloud API does
            # seem to do a read of a single register.
            print(f'holding reg {reg} now {values[0]}')
            if int(reg) == 112:
                self.dpchanged = datetime.now()

    def register_written(self, reg, value):
        print(f'holding reg {reg} now {value}')
        if int(reg) == 112:
            self.dpchanged = datetime.now()

def in_iog_slot(iog, now, hhmm):
    """Determine if we are inside an IOG charging slot"""
    if iog.timestamp + 3600 < now.timestamp():
        print("iog sensor stale ?")
        return False

    # iog uses numbered half-hour slots - 0 = 0000, 3 = 0130, etc.
    # convert hhmm into the same
    # 00 to 29 are truncated to 0, 30 to 59 rounded up
    slot = (hhmm + 20) // 50

    if iog.count >= 1 and iog.s1 <= slot < iog.e1:
        return True
    if iog.count >= 2 and iog.s2 <= slot < iog.e2:
        return True
    if iog.count >= 3 and iog.s3 <= slot < iog.e3:
        return True
    return False

async def monitor(zf = None):
    """
    Monitor the system for clipping, and adjust battery
    charging power as required.

    zf is optional stream (GZipFile) for packet-capture.
    """

    READONLY=False

    sensors = Sensors()

    # Only interested in a subset of registers, and battery
    registers = {IR(0),  IR(60), HR(0), HR(60), HR(240), HR(300)}
    plant = MyPlant(registers=registers, num_batteries=0)
    client = Client(sys.argv[1], 8899, recorder=zf, plant=plant)

    # moving averages for solar, generation export, and battery
    solar = MovingAverage()
    gen = MovingAverage()
    export = MovingAverage()
    battery = MovingAverage()

    # -1 means want to discharge, +1 means want to charge, 0 = happy
    # we use hysteresis, so only start charging when 5% below, and
    # discharging when 5% above.
    adjust_soc = 0

    # seconds since last adjustment.
    # Use -120 initially to avoid doing anything until moving averages
    # have settled down
    elapsed = -120

    await client.connect()

    # "%5d %6.1f %6.1f  %5d %6.1f %6.1f   %5d %5d %6.1f %6.1f  %s %s %d %d"
    # solar, sma, sdecay,
    # gen, gma, gdecay,
    # export, ema,
    # battery, bma, inverter.temp_inverter_heatsink,
    #                  paused, elapsed, delay)
    print("      ----------solar----------  --------gen-------    --export--   -------battery----------   -temp- -time-")

    # A latched value of whether we need to turn eco off at start of off-peak
    oeco = None

    # first time through, the refresh includes the HR's
    # subsequently, set to False to include only the IR subset.
    # (Writes from other sources will still be recorded, passively, so we will
    # find out if other clients make changes.)
    full = True

    # when cell voltages were last updated
    cells = 0

    while True:

        # use alarm as a watchdog
        signal.alarm(900)

        await client.refresh_plant(full_refresh=full, registers = registers, retries=4)
        full = False

        now = datetime.now()
        hhmm = now.hour * 100 + now.minute

        inverter = plant.inverter
        solar.update(inverter.p_pv1 + inverter.p_pv2)
        gen.update(inverter.p_inverter_out)
        export.update(inverter.p_grid_out)
        battery.update(inverter.p_battery)

        # choose a default refresh time.
        # 30s seems a good choice when solar is in the vicinity of 5kW
        # Go out to 5 mins (300s) when solar is close to 0.
        # so (5555 - solar) / 18.5 gives the right sort of shape:
        #  300.27 when solar is 0
        #   30 when solar is 5000
        # When just starting up (elapsed < 0), force delay=30

        sun = max(solar.now, solar.decay)
        delay = 30 if elapsed < 0 or sun >= 5000 else (5555 - sun) / 18.5
        assert delay >= 30

        # various key holding regs. (Names are a bit short, but they're what
        # I use for givenergy.py so they make sense to me.)
        eco = inverter.battery_power_mode
        paused = inverter.battery_pause_mode
        soc = inverter.battery_percent
        ed = inverter.enable_discharge

        # The target_soc we want to maintain. Slot #3 until 4pm, 4 until 1930, 5 until 2200, 6 until 2300, 7 thereafter

        target_soc = (
            inverter.charge_target_soc_3 if hhmm < 1600 else
            inverter.charge_target_soc_4 if hhmm < 1930 else
            inverter.charge_target_soc_5 if hhmm < 2200 else
            inverter.charge_target_soc_6 if hhmm < 2300 else
            inverter.charge_target_soc_7
        )
          

        # Read the timer values. We assume they never span midnight
        # so that we can use simple range comparisons
        # These are the raw times, so in (hour * 100 + minute) format
        ps = inverter.battery_pause_slot_1_start
        pe = inverter.battery_pause_slot_1_end
        ds = inverter.discharge_slot_3_start
        de = inverter.discharge_slot_7_end

        # we are only in full control within the pause time window
        active = ps <= hhmm < pe

        # build up the modbus modification requests in here
        requests = []
        commands = client.commands

        # task 1: figure whether we want to allow (solar) charging, or export to grid
        # aim for soc == target_soc, but allow +/- 5% hysteresis

        if hhmm > 2200 and soc > target_soc:
            # no hysteresis after 10pm
            adjust_soc = -1
        elif soc > target_soc + 5:
            # export, ie want to actively reduce Soc.
            # Note that this will be overridden below if it turns out we are in an IOG slot
            adjust_soc = -1
        elif soc > target_soc:
            if adjust_soc > 0: adjust_soc = 0  # stop charging
        elif soc == target_soc:
            adjust_soc = 0
        elif soc >= target_soc - 5:
            if adjust_soc < 0: adjust_soc = 0  # stop discharging
        else:
            adjust_soc = 1

        # if we are charging/discharging and are approaching
        # the target, poll more frequently so we don't overshoot.
        # (After sundown we are in adjust_soc > 0 with no prospect of
        #  actually reaching it, so want to suppress those extra wakeups.)
        if adjust_soc != 0 and delay > 60 and target_soc-2 <= soc <= target_soc+2:
            delay = 60


        # task 2/4: choose a setting for the eco flag


        # Set oeco if necessary - do we want to idle overnight ?
        # *TODO* if we get rid of the 'active' mode, and pause timer covers this period,
        # could use pause-discharge instead
        if hhmm >= 2330:
            # in summer, we want to idle the battery before recharging
            # but in winter, battery almost certainly ran out long before off-peak,
            # so no point wasting write cycles
            if oeco is None:
                # turn off eco only if battery > 5%
                oeco = 0 if battery.now > 5 else 1
        elif hhmm >= 100:
            # reset for tonight
            oeco = None

        # Now we can choose eco

        want_eco = eco

        if hhmm >= 2330:
            # use the overnight value we just chose
            want_eco = oeco
        elif hhmm < 100:
            # don't touch
            pass
        elif hhmm >= de:
            # after the discharge timer, eco ought to be on, since we'll
            # want dynamic discharge
            want_eco = 1
        elif adjust_soc > 0:
            # we want to allow solar charging, so eco needs to be on
            want_eco = 1
        elif eco == 1:
            # consider whether to turn it off - this is relatively straightforward
            if active and solar.now > 5000 and solar.fast > 4000 and solar.slow > 3000:
                _logger.debug("solar is high - turn off eco mode")
                want_eco = 0
        elif not active:
            # we're supposed to be inactive - turn eco back on
            # probably just drifted off the end of the pause timer ?
            _logger.debug("not active - turn on eco mode")
            want_eco = 1

        # otherwise eco is off - consider whether to turn it back on
        # This is a bit harder - don't want to turn it on prematurely
        # and have to turn it back off when sun comes out from a cloud
        # i.e. if we have decided to turn it off, really want to leave it off.
        # TODO: really ought to look to see if we are importing from grid.
        # If we are, turning eco back on seems like a priority.
        # Or just export less than 100W or so ?

        elif solar.now < 1000 and solar.fast < 1500 and solar.decay < 3000:
            _logger.debug("solar is very low - turn on eco mode")
            want_eco = 1
        elif hhmm > 1730 and solar.slow < 4000:
            # rather horrible hard-coding: likely to be cooking soon,
            # so probably ought to be in eco mode
            # discharge timer should have finished by now
            want_eco = 1
        elif ed and gen.fast >= 2000 and export.fast > 0:
            # we are discharging the battery concurrently
            # so as long as we are generating at least 2kW AC, it's probably
            # fine to leave it off for now, since the discharge will cover house load
            # But check again soon
            if delay > 60: delay = 60
        elif solar.now < 1500 and solar.fast < 2500 and solar.decay < 4000:
            # getting a little low, and no discharge to fall back on
            _logger.debug("solar is low - turn on eco mode")
            want_eco = 1
        else:
            # okay, we're leaving eco off.
            # Don't idle too long
            if delay > 60: delay = 60

        # In summer, we generally want battery-charging paused, unless eco is off
        # or adjust_soc > 0
        # In winter, not much point gratuitously pause-charging at the start of day rate,
        # since the battery will very quickly run down, and we'll need to unpause again, causing
        # unnecessary writes. Do nothing until solar slowMA has reached 100
        
        # So: pause should be 0 (not-paused) if eco is off or adjust_soc > 0 or no solar
        # and 1 (pause_charging) otherwise (eco on and adjust_soc <= 0 and solar.slow > 100)
        if active:
            want_pause = 1 if (want_eco and adjust_soc <= 0 and solar.slow >= 100) else 0

            # but we want to pause discharge during a charging slot
            # except that Octopus seems to change them constantly, so can't really rely
            # on them (unless we poll every few mins, which is not ideal)
            # As a compromise, inhibit export during a slot, but pause discharge only when
            # actually charging

            zappi = sensors.load(Sensors.Zappi)
            if zappi.status == 2 and zappi.mode == 3:
                _logger.debug("charging in eco+ mode")
                want_pause = 2                      # inhibit battery discharge
                delay = 30  # don't want to miss the end
                if adjust_soc < 0: adjust_soc = 0   # don't force export

            iog = sensors.load(Sensors.IOG)
            if in_iog_slot(iog, now, hhmm):
                _logger.debug("inside IOG slot")
                delay = 30  # don't want to miss the end
                if adjust_soc < 0: adjust_soc = 0   # don't force export

            elif iog.count > 0 and delay > 60:
                # don't want to miss the start of a pending slot
                delay = 60



        # task 3: export if necessary

        want_ed = 0

        # TODO: perhaps consider whether we're importing, ie if forced discharge + solar
        # is not enough to cover consumption. Want to go back to eco + dynamic discharge
        # in that case.

        if active and adjust_soc < 0 and ds <= hhmm <= de:
            # we want to actively discharge battery to grid
            want_ed = 1


        # commit changes. Some can be set any time, some are only while active.
        # when switching eco off, slightly better to do that before pause
        # but when switching eco on, slightly better to turn pause on first.
        # (otherwise, transitioning through eco=1, pause=0 will briefly charge the battery.)

        if eco != want_eco:
            requests.append(commands.write_named_register('battery_power_mode', want_eco))
            eco = want_eco

        if active and paused != want_pause:
            requests.append(commands.write_named_register('battery_pause_mode', want_pause))

        if active and ed != want_ed:
            requests.append(commands.write_named_register('enable_discharge', want_ed))


        _logger.info("%4d  "
                     "%5d %6.1f %6.1f %6.1f "
                     "%5d %6.1f %6.1f  "
                     "%5d %6.1f  "
                     "%5d %6.1f %s %s %3d   "
                     "%4.1f   "
                     "%d %d",
                     hhmm,
                     solar.now, solar.fast, solar.slow, solar.decay,
                     gen.now, gen.fast, gen.slow,
                     export.now, export.fast,
                     battery.now, battery.fast, paused, eco, soc,
                     inverter.temp_inverter_heatsink,
                     elapsed, delay)

        # from time to time a bogus reading causes an illegal pack operation
        try:
            sensors.store(Sensors.GivEnergy(int(now.timestamp()),
                                        solar.now, int(solar.fast),
                                        export.now, int(export.fast),
                                        battery.now, int(battery.fast),
                                        soc,
                                        gen.now, int(gen.fast),
                                        inverter.p_eps_backup))
        except:
           print("failed to update sensor")

        if len(requests) > 0:
            if READONLY or elapsed < 0:
                print(requests)
            else:
                elapsed = 0
                client.execute(requests, timeout=2.0, retries=2, return_exceptions = True)

        if not 600 < hhmm < 2200 and not hhmm < cells < hhmm+10:
            # record cell voltages every 10 mins between 10pm and 6am
            cells = hhmm
            batt = plant.batteries[0]
            _logger.info("%4d  BATT:" +  " %6.3f" * 16,
                         hhmm,
                         batt.v_cell_01,
                         batt.v_cell_02,
                         batt.v_cell_03,
                         batt.v_cell_04,
                         batt.v_cell_05,
                         batt.v_cell_06,
                         batt.v_cell_07,
                         batt.v_cell_08,
                         batt.v_cell_09,
                         batt.v_cell_10,
                         batt.v_cell_11,
                         batt.v_cell_12,
                         batt.v_cell_13,
                         batt.v_cell_14,
                         batt.v_cell_15,
                         batt.v_cell_16,
                         )

        # after all the effort to tune the delay, just fix it at
        # 30 while Daikin is using the ac info.
        # NOT NEEDED DURING THE SUMMER
        # delay = 30

        await asyncio.sleep(delay)
        elapsed += delay  # only needs to be approx

if __name__ == "__main__":

    now = datetime.now()
    tstamp = now.strftime("%Y%m%d-%H%M")

    # log to both file and console
    zf = gzip.open(filename="/tmp/monitor." + tstamp + ".log.gz", mode="wt")
    file_log_handler = logging.StreamHandler(zf)
    _logger.addHandler(file_log_handler)

    stderr_log_handler = logging.StreamHandler()
    _logger.addHandler(stderr_log_handler)

    # nice output format
    formatter = logging.Formatter(fmt="%(asctime)s: %(message)s", datefmt="%Y-%m-%d")
    file_log_handler.setFormatter(formatter)

    _logger.setLevel(logging.DEBUG)

    zf = gzip.GzipFile(filename="/tmp/capture." + tstamp + ".gz", mode="wb")
    asyncio.run(monitor(zf))
