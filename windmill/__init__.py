"""Local dashboard, controller and watchdog for Windmill Air AC units.

Windmill publishes no web dashboard and no API. `dashboard.windmillair.com` is
a white-labelled Blynk deployment whose HTTP API accepts each unit's device
token directly, and that is what this reads and writes.

Layout:

    config    settings, and the split between code paths and data paths
    store     SQLite: telemetry, rollups, settings, audit, schedules
    blynk     the upstream client, and the documented shape of that API

    energy    integration and range queries        analysis  weather normalising
    meter     our figures against the units' own   calibrate which rule to trust
    forecast  where this month lands               overrun   cooling with no demand
    filters   when a filter needs changing         guard     the software thermostat
    schedule  time-of-day rules                    notify    alerts worth a person
    identify  binding an unnamed datastream        backup    database snapshots
    backfill  seeding history from the vendor      gapfill   reconstructing gaps
    repair    recovering, then reconstructing      server    HTTP + the watchdog

`server` is the only module that acts on its own; everything else answers
questions. Run it with `python -m windmill`.
"""

__version__ = "1.0.0"
