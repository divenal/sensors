#!/usr/bin/env python3

# A script to fetch greener days forecast and store to sensor
# This one just runs one-shot from cron

from datetime import datetime

from octopus import GraphQL
from sensors import Sensors


def main():

    sensors = Sensors()
    graph = GraphQL();

    greener = graph.greener_days()
    # eg {'greennessForecast': [{'validFrom': '2025-04-27T22:00:00+00:00', 'validTo': '2025-04-28T05:00:00+00:00', 'greennessScore': 37, 'greennessIndex': 'MEDIUM', 'highlightFlag': True},
    # entries should already be sorted, but lets be sure
    entries = sorted(greener["greennessForecast"], key=lambda entry: entry['validFrom'])
    vf = datetime.fromisoformat(entries[0]['validFrom'])
    scores = (entry['greennessScore'] for entry in entries)
    g = Sensors.GreenerDays(int(vf.timestamp()), *scores)
    sensors.store(g)

if __name__ == "__main__":
    main()
