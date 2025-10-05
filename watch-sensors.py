#!/usr/bin/env python3

# Watch sensor timestamps to try to detect breaking scripts
# And some other misc. alerts

import signal
import os
import requests
import time

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
def notify(notifier, now, msg, priority="4"):
    """Conditionally send a notification, updating notification time."""

    if notifier is not None:
        if notifier.notified == 0:
            pass  # not previously notified
        elif now < notifier.notified + notifier.interval:
            return # notified recently
        notifier.notified = now
        
    # This originally used givenergy notifications, but they're forbidding it now ?
    # Ah - no, I'd forgotten to update the API key !
    print(msg)
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
watchlist = (Sensors.Zappi, Sensors.Daikin, Sensors.GivEnergy, Sensors.IOG, Sensors.GreenerDays)

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
    if zappi.status != zstatus.old:
        if zappi.status < 0:
            notify(zstatus, now, "zappi has faulted ?")
        else:
            zstatus.notifed = 0
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
        signal.alarm(300)

        now = time.time()
        for sc in watchlist:
            s = sensors.load(sc)
            check_sensor(s, now)

        check_zappi(sensors, now)
        check_ge(sensors, now)
        check_maple(now)

        time.sleep(30)

if __name__ == "__main__":
    main()
