#!/usr/bin/env python3

# Watch sensor timestamps to try to detect breaking scripts
# And some other misc. alerts

import signal
import os
import requests
import time

from sensors import Sensors

watchlist = (Sensors.Zappi, Sensors.Daikin, Sensors.GivEnergy, Sensors.IOG, Sensors.GreenerDays)

# for each interesting sensor, stash a expected max update interval (seconds)
# and set a notified time of 0. We only want to notify once, until
# it starts working again, so the convention is that if last timestamp
# is newer than notified time, it must have woken up then died again.
# But while notified time is newer than instance timestamp, no change
# since we last notified.

# for some reason, zappi goes offline when we turn off maple's ethernet
# takes about 15 mins to recover ?
Sensors.Zappi.update = 900
Sensors.Daikin.update = 2100  # 35 mins
Sensors.GivEnergy.update = 600
Sensors.IOG.update = 4000
Sensors.GreenerDays.update = 4*60*60*24

# a few other things we stash
Sensors.Zappi.oldhp = 0
Sensors.Zappi.oldstatus = 0
Sensors.Zappi.oldlock = 23

Sensors.GivEnergy.eps = 0

for s in watchlist:
    s.notified = 0


def notify(msg, priority="4"):
    # This originally used givenergy notifications, but they're forbidding it now ?
    # Ah - no, I'd forgotten to update the API key !
    print(msg)
    requests.post("https://ntfy.sh/divenal14_sensors",
                  data=msg.encode(encoding='utf-8'),
                  headers={ "Tags": "warning", "Priority": priority })

def notify_sensor(sn, age):

    if age == 0:
        notify(f"sensor {sn} is back online", "3")
        return

    # turn age in seconds into something more friendly
    if age > 7200:
        when = f"{age / 3600:.2f} hours"
    elif age > 300:
        when = f"{age / 60:.2f} minutes"
    else:
        when = "f{age:d} seconds"

    notify(f"sensor {sn} has not been updated for {when}")

def check_sensor(s, now):
    """If sensor of given class is stale, send a notification"""
    sc = s.__class__
    if s.timestamp + sc.update < now:
        # sensor is stale
        if sc.notified < s.timestamp or sc.notified + 8*3600 < now:
            # either have not notified, or notified 8 hours ago
            notify_sensor(sc.__name__, now - s.timestamp)
            sc.notified = now
    elif sc.notified != 0:
        # sensor has recovered since notification was sent
        notify_sensor(sc.__name__, 0)
        sc.notified = 0

def check_zappi(sensors, now):
    zappi = sensors.load(Sensors.Zappi)
    if zappi.status != zappi.oldstatus:
        if zappi.status < 0:
            notify("zappi has faulted ?")
        Sensors.Zappi.oldstatus = zappi.status
    if zappi.lock != zappi.oldlock:
        # lock is usually 23 when locked, but sometimes 31 ?   22 seems to be unlocked.
        if zappi.lock != 23 and zappi.lock != 31:
            notify("zappi not locked ?")
        Sensors.Zappi.oldlock = zappi.lock

def check_ge(sensors, now):
    ge = sensors.load(Sensors.GivEnergy)
    if ge.eps > 0:
        if Sensors.GivEnergy.eps + 3600 < now:
            notify("givenergy outage ?")
            Sensors.GivEnergy.eps = now
    else:
        Sensors.GivEnergy.eps = 0

def check_hp(sensors, now):
    """Look to see if hp has turned on/off in the last interval."""
    # previous hp power is stashed in the Sensors.Zappi (class) object.
    # Assume a min power of 500W when HP is on.
    zappi = sensors.load(Sensors.Zappi)
    # print("zappi before/after: ", Sensors.Zappi.oldhp, zappi.hp)
    if Sensors.Zappi.oldhp > 500 and zappi.hp < 500:
        notify("hp has turned off ?")
    elif Sensors.Zappi.oldhp < 500 and zappi.hp > 500:
        notify("hp has turned on ?")
    Sensors.Zappi.oldhp = zappi.hp
       
def check_fs(sensors, now):
    svfs = os.statvfs("/extra/hts")
    if svfs.f_bavail * svfs.f_bsize < 4*1024*1024*1024:
        # only 4Gb free
        if sensors.fs_notified + 8*3600 < now:
            # last notification was more than 8 hours ago
            notify("less than 4Gb space available")
            sensors.fs_notified = now
    else:
        sensors.fs_notified = 0

def main():
    sensors = Sensors()

    # hang timestamp of disk notification off that
    # (just because python makes a fuss about globals)
    sensors.fs_notified = 0

    while True:

        # use alarm as a watchdog to stop script getting stuck somehow
        signal.alarm(300)

        now = time.time()
        for sc in watchlist:
            s = sensors.load(sc)
            check_sensor(s, now)

        # check_hp(sensors, now)
        check_zappi(sensors, now)
        check_ge(sensors, now)
        check_fs(sensors, now)

        # TODO: add a check for loadavg, which goes a bit
        # bonkers from time to time
        time.sleep(30)

if __name__ == "__main__":
    main()
