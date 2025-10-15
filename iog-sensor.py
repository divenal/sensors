#!/usr/bin/env python3

# A script to process IOG slots and record any bonus ones
# Times are represented in units of 30 minutes in local timezone (ie +1 hour in bst):
#  0 = 00:00
#  1 = 00:30
# 11 = 05:30
# 47 = 23:30

import signal
import logging
import gzip
from datetime import datetime
import time

from octopus import IOG
from sensors import Sensors

_logger = logging.getLogger(__name__)

def simplify(d):
    """Return a simplified dispatch, where times are reduced to multiples of 30 minutes"""

    # dispatches are provided with timezone info (currently utc). Shift them to local timezone

    s = datetime.fromisoformat(d["start"]).astimezone()
    sh = s.hour
    sm = s.minute

    # Note that rounding up may result in time of 48 rather than 0.
    # eg start of 23:55 will become half-hour slot number 48

    # start time is usually rounded down, but not if mins are very close to end of half-hour period
    start = sh * 2
    if sm >= 55:
        start = start + 2   # round up to next hour
    elif sm >= 25:
        start = start + 1   # round to half hour
    # else round down to start of hour
    
    e = datetime.fromisoformat(d["end"]).astimezone()
    eh = e.hour
    em = e.minute

    # end is usually rounded up, unless it's just over the half-hour boundary
    end = eh * 2
    if em >= 35:
        end = end + 2  # round to next hour
    elif em >= 5:
        end = end + 1
    # else round down to whole hour

    return (start, end)

def process(pending):

    # first, sort using string form of times
    pending.sort(key = lambda d : d["start"])
    for p in pending:
        _logger.debug(p)

    # then convert to simplified times
    pending = [simplify(d) for d in pending]
    #print(pending)

    # Now split each piece into up to 3:
    #  A) from 00:00 to 05:30  (0 to 11)
    #  B) from 05:30 to 23:30  (11 to 47)
    #  C) from 23:30 to 00:00  (47 to 0/48)
    # and discard (A) and (C)

    # Iterate backwards so that creating new pieces doesn't break the index
    # Now go through and turn each segment into 0, 1 or 2 pieces
    # Ignore the possibility that we charge for 18 hours, from cheap rate of
    # one day to cheap rate of next day

    for after in range(len(pending),0,-1):
        (a, b) = pending[after - 1]
        if a == b:
            # probably very short slot got rounded to empty
            result = ()
        elif 11 <= a < 47:
            if 11 < b <= 47:
                if a < b:
                    # entirely during the day - nothing to change
                    continue
                else:
                    # starts before 23:30 and ends after 05:30 so need to split
                    result = ((a, 47), (11, b))
            else:
                # a is daytime, b is nighttime
                result = ( (a, 47), )

        elif 11 < b <= 47:
            # a is nighttime, b is daytime
            result = ( (11, b), )

        else:
            # both are nighttime
            result = ()

        pending[after-1:after] = result

    #print(pending)

    # Finally, merge adjacent entries.
    # Again, iterate backwards through the list - this way,
    # even if we delete some entries, we don't lose
    # our place

    for idx in range(len(pending)-1,0,-1):
        before = pending[idx-1]
        after = pending[idx]
        if (before[0] <= after[0] <= before[1]):
            pending[idx-1:idx+1] = ((before[0], after[1]),)

    return pending


def main():
    """Maintain the iog state in the Sensors memory-map."""

    sensors = Sensors()
    iog = IOG()

    total = 0       # total number of pending slots
    last = 0        # last fetch from graphql
    seen = 0        # non-zero if we have seen any dispatches since plugin
    zstatus = 0     # last zappi status

    while True:

        signal.alarm(3600)
        now = datetime.now()
        ts = int(now.timestamp())
        delta = ts - last

        zappi = sensors.load(Sensors.Zappi)
        if zappi.status <= 0 or zappi.mode != 3 :
            # not plugged in, or not in eco+ mode (visitor charging in fast mode, perhaps ?)
            total = 0
            seen = 0
            s = Sensors.IOG(ts, 0, 0)
            sensors.store(s)
        else:

            # choose an appropriate increment based on the state
            if not 529 < now.hour * 100 + now.minute < 2330:
                # from 23:30 to 05:30 - once an hour is adequate
                delay = 3600
            elif zappi.status != zstatus :
                # a change of state - either just plugged in, or started or stopped charging
                # update immediately
                delay = 0
            elif now.minute % 30 < 6:
                # try to update around about the top of the half-hour
                  # (unless we've updated in the last 5 minutes)
                delay = 300
            else:
                delay = 3600

            if last + delay <= ts:
                dispatches = iog.getDispatches()
                last = ts
                planned = dispatches['plannedDispatches']
                total = len(planned)
                seen = seen | total
                planned = process(planned)
                # print(planned)

                count = len(planned)
                if count > 3:
                    count = 3
                    planned = planned[0:3]
                args=[ts, total, count]
                for (a, b) in planned:
                    args.append(a)
                    args.append(b)
                s = Sensors.IOG(*args)
                _logger.info(s)
                sensors.store(s)

        zstatus = zappi.status
        time.sleep(120)

def test():
    # some test data
    t = [{'start': '2024-11-16T22:30:00+00:00', 'end': '2024-11-17T01:00:00+00:00', 'delta': '-1.77'},
          {'start': '2024-11-17T04:30:00+00:00', 'end': '2024-11-17T05:00:00+00:00', 'delta': '-1.77'},
          {'start': '2024-11-17T05:00:00+00:00', 'end': '2024-11-17T06:30:00+00:00', 'delta': '-1.12'}]
    out = process(t)
    print(out)

    t = [{'start': '2024-11-17T05:00:00+00:00', 'end': '2024-11-17T06:00:00+00:00', 'delta': '-1.77'},
         {'start': '2024-11-17T06:30:00+00:00', 'end': '2024-11-17T06:45:00+00:00', 'delta': '-0.89'},
         {'start': '2024-11-17T06:45:00+00:00', 'end': '2024-11-17T07:00:00+00:00', 'delta': '-0.23'},
         {'start': '2024-11-17T07:00:00+00:00', 'end': '2024-11-17T07:30:00+00:00', 'delta': '-1.77'}]
    out = process(t)
    print(out)

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

    now = datetime.now()
    tstamp = now.strftime("%Y%m%d-%H%M")

    with gzip.open(filename="/tmp/iog." + tstamp + ".log.gz", mode="wt") as zf:
        setup_logging(zf)
        main()
