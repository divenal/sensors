#!/usr/bin/env python3

# Watch sensor timestamps to try to detect breaking scripts
# And some other misc. alerts

import os
import requests
import time
import datetime
from signal import alarm

from sensors import Sensors

# A notifier contains a notified field and an interval field.
# former records when it last notified, and interval is how often it should notify.
# Notification is then throttled to only that interval

# This class is just a generic container, but any object will do. For Sensors, the
# sensor class itself is used.
# Arbitary values can be attached to it

class Notifier:
    notified = 0
    interval = 8*60*60
    def __init__(self, interval):
        self.interval = interval

# Because any object can be a notifier, this isn't a member function
def notify(notifier, now, msg, priority=None):
    """Conditionally send a notification, updating notification time."""

    if notifier is not None:
        if notifier.notified == 0:
            pass  # not previously notified
        elif now < notifier.notified + notifier.interval:
            return # notified recently
        notifier.notified = now
        
    # This originally used givenergy notifications, but they're forbidding it now ?
    # Ah - no, I'd forgotten to update the API key !
    d = datetime.datetime.fromtimestamp(now)
    print(d.isoformat(timespec='seconds'), msg)
    if priority is None:
        # choose depending on time of day
        hhmm = d.hour * 100 + d.minute
        priority = "4" if 700 < hhmm < 2230 else "3"

    requests.post("https://ntfy.sh/divenal14_sensors",
                  data=msg.encode(encoding='utf-8'),
                  headers={ "Tags": "warning", "Priority": priority })

def pretty(age):
    """Given an age in seconds, return a helpful string."""
    if age > 7200:
        return f"{age / 3600:.2f} hours"
    elif age > 300:
        return f"{age / 60:.2f} minutes"
    else:
        return "f{age:d} seconds"

# The sensors we watch
watchlist = (Sensors.Zappi, Sensors.Daikin, Sensors.GivEnergy, Sensors.IOG, Sensors.GreenerDays, Sensors.Doit)

for s in watchlist:
    s.notified = 0
    s.interval = 8*60*60

# Give each sensor class (the notifer) an update interval within which
# they are expected to have updated.

# for some reason, zappi goes offline when we turn off maple's ethernet
# takes about 15 mins to recover ?
Sensors.Zappi.update = 900
Sensors.Daikin.update = 2100  # 35 mins
Sensors.GivEnergy.update = 600
Sensors.IOG.update = 4000
Sensors.GreenerDays.update = 4*60*60*24
Sensors.Doit.update = 300

# A notifier for inverter tripping (non-zero eps)
eps = Notifier(30*60)

# zappi fault status
zstatus = Notifier(10*60)
zstatus.old = 0

# zappi lock
zlock = Notifier(10*60)
zlock.old = 23

# hp power - not really a notifier, just somewhere to stash old value
hp = Notifier(10*60*60)
hp.old = 0

# Stash the first bonus charging slot here
Sensors.IOG.first = 0

# filesystem
fs = Notifier(4*60*60)

def check_sensor(s, now):
    """If sensor of given class is stale, send a notification"""
    sc = s.__class__
    if now > s.timestamp + sc.update:
        age = pretty(now - s.timestamp)
        notify(sc, now, f"{sc.__name__} has not updated for {age}")
    elif (sc.notified):
        notify(None, now, f"{sc.__name__} back online", "3")
        sc.notified = 0

def check_zappi(sensors, now):
    zappi = sensors.load(Sensors.Zappi)
    old = zstatus.old
    if zappi.status != old:
        if zappi.status < 0:
            notify(zstatus, now, "zappi has faulted ?")
        else:
            zstatus.notifed = 0

        if zappi.status == 2 or old == 2:
            # car has started or stopped charging
            d = datetime.datetime.fromtimestamp(now)
            hhmm = d.hour * 100 + d.minute
            if 700 <= hhmm <= 2200:
                notify(None, now,
                       "zappi is charging" if zappi.status == 2 else "zappi stopped charging",
                       "4")
        zstatus.old = zappi.status

    if zappi.lock != zlock.old:
        # lock is usually 23 when locked, but sometimes 31 ?   22 seems to be unlocked.
        if zappi.lock != 23 and zappi.lock != 31:
            notify(zlock, now, "zappi not locked ?")
        elif zlock.notified:
            notify(None, now, "zappi locked", "3")
            zlock.notified = 0

        # This was really just for testing
        # Look to see if daikin has turned on/off recently
        return
        if hp.old > 500 and zappi.hp < 500:
            notify("hp has turned off ?")
        elif hp.old < 500 and zappi.hp > 500:
            notify("hp has turned on ?")
        hp.old = zappi.hp
        
def check_iog(sensors, now):
    """Alert me about charging slots during the day."""
    iog = sensors.load(Sensors.IOG)
    old = Sensors.IOG.first
    first = iog.s1 if iog.count >= 1 else 0
    if first != old:
        # something has changed. Figure out if its interesting
        # IOG sensor counts in half-hour periods, 0 = 00:00, 1 = 00:30, 47 = 23:30
        # It doesn't store charging periods between 23:30 and 05:30
        # Octopus only alerts about slots in the next 12 hours, I think,
        # (Octopus always schedule in UTC, but the IOG download stuff converts to local timezone.)
        # *TODO: Perhaps consider the IOG day to run 11am to 11am, so for times less than 11am, add 24 hours.
        # That makes it easier to tell whether a time is in the future or the past.

        d = datetime.datetime.fromtimestamp(now)
        p = d.hour * 2 + d.minute // 30

        if old > 22 and old < p:
            old = 0    # a slot after 11am is in the past. Probably just dropped off the schedule ?

        if old >= p and first == 0:
            oh = old // 2
            om = (old & 1) * 30
            notify(None, now, f"charging slot at {oh:02d}:{om:02d} cancelled")
        elif first <= 14:
            pass  # just ignore anything before 7am
        elif first <= 22 and p >= 28:
            pass  # it's after 2pm now, and first charging slot is before 11am, ie tomorrow morning
        elif first >= p:
            fh = first // 2
            fm = 30 * (first & 1)
            notify(None, now, f"bonus charging slot at {fh:2d}:{fm:02d}")
        Sensors.IOG.first = first

def check_ge(sensors, now):
    ge = sensors.load(Sensors.GivEnergy)
    if ge.eps > 0:
        notify(eps, now, "givenergy outage ?")
    elif (eps.notified):
        notify(None, now, "givenergy back");
        eps.notified = 0

       
def check_maple(now):
    svfs = os.statvfs("/extra/hts")
    if svfs.f_bavail * svfs.f_bsize < 4*1024*1024*1024:
        notify(fs, now, "less than 4Gb space available")
    else:
        fs.notified = 0

    # TODO: add a check for loadavg, which goes a bit
    # bonkers from time to time

def main():
    sensors = Sensors()

    while True:

        # use alarm as a watchdog to stop script getting stuck somehow
        alarm(300)

        now = time.time()
        for sc in watchlist:
            s = sensors.load(sc)
            check_sensor(s, now)

        check_zappi(sensors, now)
        check_ge(sensors, now)
        check_iog(sensors, now)
        check_maple(now)
        time.sleep(30)

if __name__ == "__main__":
    main()
