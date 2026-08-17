#!/usr/bin/env python3
"""Bundle the telemetry behind the fault report, for Windmill's engineers.

    python3 -m tools.export_support                  # -> support-export/
    python3 -m tools.export_support --out /tmp/x     # somewhere else
    python3 -m tools.export_support --zip            # ...and zip it

Writes CSVs, the schema, and the exact query behind every figure in the report,
so a number can be checked rather than taken on trust.

The device tokens are the reason this is a script rather than a copy of
`windmill.db`. A token grants full control of an AC unit, `units.token` holds
seven of them, and "send them the database" is how they would leak. Nothing
here reads that column. The Blynk owner email and the household's public IP
come back from the device endpoint and are not stored locally, so they cannot
escape this way either -- `devices` holds firmware fields only.
"""
import argparse
import csv
import pathlib
import shutil
import sqlite3
import time

from windmill import store


#: What goes in the bundle. Ordered so the small, readable tables come first --
#: someone opening this cold should meet the datastream map before a million
#: rows of `readings`.
TABLES = [
    ("units", """
        SELECT name AS unit, label, enabled, automatic,
               datetime(added_at,'unixepoch','localtime') AS added
        FROM units ORDER BY name"""),
    ("devices", """
        SELECT unit, device_id, fw_version, fw_build, blynk_version, board,
               datetime(activated_at,'unixepoch','localtime') AS activated,
               datetime(fw_updated_at,'unixepoch','localtime') AS fw_updated
        FROM devices ORDER BY unit"""),
    ("floors", "SELECT unit, floor FROM unit_floors ORDER BY floor, unit"),
    ("audit", """
        SELECT datetime(ts,'unixepoch','localtime') AS at, ts AS epoch,
               source, target AS unit, action, detail, ok
        FROM audit ORDER BY ts"""),
    ("sync_events", """
        SELECT datetime(ts,'unixepoch','localtime') AS at, ts AS epoch,
               unit, state, action, detail
        FROM sync_events ORDER BY ts"""),
    ("hourly", """
        SELECT unit, datetime(hour,'unixepoch','localtime') AS hour_local,
               hour AS epoch, wh, w_avg, w_max, on_s, cooling_s, temp_f,
               samples, estimated, method
        FROM hourly ORDER BY hour, unit"""),
    ("readings", """
        SELECT unit, pin, datetime(ts,'unixepoch','localtime') AS at,
               ts AS epoch, val
        FROM readings ORDER BY ts, unit, pin"""),
]

#: Windmill's own names for the datastreams, read from the CSV export headers.
#: Without this the readings file is a wall of pin codes.
PINS = [
    ("v0",  "Power Switch",           "1",  "0 off / 1 on"),
    ("v1",  "Room Temperature",       "2",  "degrees F, as the unit reports it"),
    ("v2",  "Set Temperature",        "3",  "degrees F"),
    ("v3",  "Mode",                   "",   "0 Fan / 1 Cool / 2 Eco (unconfirmed ids)"),
    ("v4",  "Fan Speed",              "",   "0 Auto / 1 Low / 2 Med / 3 High (unconfirmed ids)"),
    ("v7",  "Filter Runtime (hours)", "9",  ""),
    ("v11", "Filter Change Indicator", "12", ""),
    ("v15", "Power (watts)",          "7",  "confirmed against a minute-granularity export"),
    ("v16", "Energy",                 "",   "unconfirmed"),
    ("v30", "WiFi RSSI",              "14", "dBm"),
    ("v31", "Protocol Failure Count", "15", "cumulative, monotonic, never resets"),
    ("c0",  "Cloud session",          "",   "SYNTHETIC -- our record of "
                                            "isHardwareConnected, not a Windmill pin"),
]

#: Every figure quoted in the report, as the query that produced it. The point
#: is that none of them has to be taken on trust.
QUERIES = """\
-- Queries behind every figure in the fault report.
-- Run against the bundled windmill schema, or read them as documentation.

-- ---------------------------------------------------------------- fleet
-- Firmware split. Three units on 0.4.4, four on 0.4.7.
SELECT unit, fw_version, fw_build, blynk_version, board,
       datetime(activated_at,'unixepoch','localtime') activated
FROM devices ORDER BY fw_version, unit;

-- Average WiFi RSSI per unit (dBm).
SELECT unit, ROUND(AVG(val),1) avg_rssi, MIN(val) worst, MAX(val) best, COUNT(*) n
FROM readings WHERE pin='v30' GROUP BY unit ORDER BY avg_rssi DESC;

-- Protocol Failure Count: the counter is cumulative, so the rate is the climb
-- over the window. This is the "fails/h" column.
SELECT unit, MIN(val) lo, MAX(val) hi, MAX(val)-MIN(val) climbed,
       ROUND((MAX(val)-MIN(val))*1.0/((MAX(ts)-MIN(ts))/3600.0),2) per_hour,
       ROUND((MAX(ts)-MIN(ts))/86400.0,1) days
FROM readings WHERE pin='v31' GROUP BY unit ORDER BY per_hour DESC;

-- ------------------------------------------------------------ finding 01
-- 157 reversions: a commanded setting the unit moved away from on its own.
-- Recorded only inside the settling window after a settings write, and never
-- while our own guard is holding the unit -- see README.
SELECT COUNT(*) FROM audit WHERE action='drifted after a resync';

-- ...by unit.
SELECT target unit, COUNT(*) n FROM audit
WHERE action='drifted after a resync' GROUP BY target ORDER BY n DESC;

-- ...and what state each unit had moved to. Cool->Fan and Auto->Low dominate.
SELECT detail, COUNT(*) n FROM audit
WHERE action='drifted after a resync' GROUP BY detail ORDER BY n DESC;

-- 33 unprompted moves into Eco.
SELECT target unit, COUNT(*) n FROM audit
WHERE action='took it out of Eco' GROUP BY target ORDER BY n DESC;

-- Zero cloud session drops over the same window: c0 never went 1 -> 0.
SELECT unit, COUNT(*) samples, SUM(val=0) offline_samples,
       datetime(MIN(ts),'unixepoch','localtime') first,
       datetime(MAX(ts),'unixepoch','localtime') last
FROM readings WHERE pin='c0' GROUP BY unit;

-- ------------------------------------------------------------ finding 03
-- Units that powered themselves off within seconds of accepting a write.
SELECT datetime(ts,'unixepoch','localtime') at, target unit, detail
FROM audit WHERE action='switched itself off after a write' ORDER BY ts;

-- ------------------------------------------------------------ finding 02
-- Blower stopped while the unit reports on: powered on, drawing under 8 W.
-- (42/43/44 W are the three fan speeds; a stopped blower draws 0.)
SELECT unit, datetime(ts,'unixepoch','localtime') at, val watts
FROM readings WHERE pin='v15' AND val < 8
ORDER BY unit, ts;

-- Overcooling is computed in windmill/overrun.py rather than in SQL: it needs
-- watts, room temp and setpoint interpolated onto a common timeline, and it
-- only counts time the unit was actually running. See assess() there.
"""

README = """\
# Windmill fault report -- raw data

Telemetry behind the fault report, from seven Windmill AC units in one house.
Everything is CSV; `queries.sql` holds the exact query behind every figure
quoted in the report.

## What collected this

A local service polls all seven units every 30 seconds through the Blynk HTTP
API (`/external/api/getAll`, plus `isHardwareConnected` per unit) and writes
every value that changed to SQLite, with a keyframe every 15 minutes regardless.
AC state is mostly static, so storing changes rather than samples compresses
roughly 18x -- but it means **a row is a change, not a tick**. To read a value
at an arbitrary moment, take the last row at or before it.

## Files

| File | Rows are |
|---|---|
| `pins.csv` | the datastream map -- read this first |
| `units.csv` | the seven units, by room |
| `devices.csv` | firmware, Blynk library and activation date per unit |
| `floors.csv` | which floor each unit is on |
| `readings.csv` | every datastream change, all units, all pins |
| `hourly.csv` | per-unit energy rollup, one row per unit per hour |
| `audit.csv` | every action taken, by a person or by the watchdog, and whether it worked |
| `sync_events.csv` | what the sync watchdog saw and concluded |
| `queries.sql` | the query behind every figure in the report |

Timestamps appear twice: `at`/`hour_local` as local time for reading, and
`epoch` as a Unix timestamp for joining. Local time is US Eastern.

## Two windows

* **Datastream history** -- from 2026-07-17. This is `readings` and `hourly`.
* **Connection and command instrumentation** -- from 2026-08-16. This is
  `audit`, `sync_events`, and pin `c0`. The reversion and session figures in
  the report cover this shorter window.

## Reading `audit.csv`

`source` is who acted: `you` (a person, through the dashboard), `watchdog`
(automatic sync/drift correction), or `guard` (automatic overcooling
protection). The actions that matter to the report:

| action | means |
|---|---|
| `drifted after a resync` | the unit moved off a setting we had just written -- **this is the reversion count** |
| `took it out of Eco` | the unit had put itself in Eco with no command to do so |
| `switched itself off after a write` | settings accepted by the cloud, unit powered off anyway |
| `kept setting` | the unit had drifted and the setting was re-sent |
| `reset to defaults` | the unit refused its saved settings repeatedly, so it was put somewhere sane |
| `gave up keeping` | it would not hold a setting at all and was left alone |

`detail` records the state the unit was actually found in, so every reversion
can be checked individually rather than trusted in aggregate.

## Two honesty notes

**Our own software also drives these units.** It runs a watchdog that re-sends
drifted settings and a guard that puts an overcooling unit into fan-only mode.
So some state changes in this data are ours. The reversion count excludes them:
`drifted after a resync` is only recorded inside the settling window following
a settings write, and never while the guard is holding that unit. If you want
the unfiltered picture, `audit.csv` has every action we took, with its source.

**A setting read back from `getAll` proves nothing.** The cloud echoes a write
whether or not the hardware applied it, which is why nothing here treats a
successful write as evidence the unit did anything. Only values the unit
pushed are used as evidence.

## Not included

Device tokens. `units.token` holds seven of them and a token grants full
control of an AC unit, so no export from this tool reads that column. If you
need to correlate against your own records, `devices.csv` carries the Blynk
device ids.
"""


def dump(con, out, name, sql):
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    path = out / f"{name}.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        n = 0
        for row in cur:
            w.writerow(["" if v is None else v for v in row])
            n += 1
    return path, n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="support-export",
                   help="directory to write (default: support-export)")
    p.add_argument("--zip", action="store_true", help="also produce a .zip")
    args = p.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    con = store.connect()

    print(f"  {'file':<22} {'rows':>9}")
    total = 0
    for name, sql in TABLES:
        try:
            path, n = dump(con, out, name, sql)
        except sqlite3.Error as exc:
            print(f"  {name:<22} {'--':>9}  skipped: {exc}")
            continue
        total += n
        print(f"  {path.name:<22} {n:>9,}")

    with (out / "pins.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["pin", "windmill_name", "export_id", "notes"])
        w.writerows(PINS)
    print(f"  {'pins.csv':<22} {len(PINS):>9,}")

    (out / "queries.sql").write_text(QUERIES)
    (out / "README.md").write_text(README)
    print(f"  {'queries.sql':<22} {'--':>9}")
    print(f"  {'README.md':<22} {'--':>9}")

    # Belt and braces. The whole point of building the bundle column by column
    # is that a token cannot reach it; this is what catches a future edit that
    # adds `SELECT *` to TABLES.
    tokens = {r["token"] for r in con.execute("SELECT token FROM units")
              if r["token"]}
    leaked = [f.name for f in out.iterdir() if f.is_file()
              and any(t in f.read_text(errors="ignore") for t in tokens)]
    if leaked:
        raise SystemExit(f"\n  ABORT: device token found in {', '.join(leaked)}. "
                         f"Not shipping this.")
    con.close()

    size = sum(f.stat().st_size for f in out.iterdir() if f.is_file())
    print(f"\n  {total:,} rows, {size / 1e6:.1f} MB in {out}/")
    print(f"  checked for device tokens: none present")

    if args.zip:
        stamp = time.strftime("%Y-%m-%d")
        archive = shutil.make_archive(f"windmill-telemetry-{stamp}", "zip", out)
        print(f"  {archive}  ({pathlib.Path(archive).stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
