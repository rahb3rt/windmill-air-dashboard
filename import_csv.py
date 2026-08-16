#!/usr/bin/env python3
"""Import a CSV exported by hand from the Windmill dashboard widget.

    python3 import_csv.py --unit Kitchen "~/Downloads/widget_Power and Room T....csv"

The HTTP export API is capped at 72 reports per device per day and only offers
hourly resolution over a month. The dashboard's own widget download has neither
limit: it comes at the device's native ~10 second cadence, which is finer than
this tool's own 30 second polling. When a unit's quota is exhausted, downloading
by hand and importing here is strictly better than waiting.

The file is sparse -- one column populated per row, at whatever cadence each
datastream reports -- so values are stored as readings and the hourly rollup is
rebuilt from them, exactly as locally sampled data is. Imported hours are real
measurements, so they clear any estimate previously covering them.

Timestamps are naive local time; they are read in the machine's zone, which is
correct as long as you import on the machine that logged them.
"""
import argparse
import csv
import pathlib
import time

import config
import energy
import store

#: CSV column header -> virtual pin. Names come from the widget export.
COLUMNS = {
    "Room Temperature": "v1",
    "Set Temperature": "v2",
    "Power": "v15",
    "Mode": "v3",
    "Fan Speed": "v4",
    "Energy": "v16",
    "Filter Runtime": "v7",
}


def parse_ts(raw):
    raw = raw.strip().strip('"').strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(time.mktime(time.strptime(raw, fmt)))
        except ValueError:
            continue
    return None


def read_rows(path):
    """-> ([(ts, pin, value)], set(unknown columns))"""
    out, unknown = [], set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for field in reader.fieldnames or []:
            if field and field != "Timestamp" and field not in COLUMNS:
                unknown.add(field)
        for row in reader:
            ts = parse_ts(row.get("Timestamp") or "")
            if ts is None:
                continue
            for col, pin in COLUMNS.items():
                raw = (row.get(col) or "").strip()
                if not raw:
                    continue
                try:
                    out.append((ts, pin, float(raw)))
                except ValueError:
                    continue
    out.sort()
    return out, unknown


def import_file(con, unit, path, dry_run=False):
    if not any(d["name"] == unit for d in config.DEVICES):
        raise SystemExit(f"unknown unit {unit!r}; known: "
                         f"{', '.join(d['name'] for d in config.DEVICES)}")
    rows, unknown = read_rows(path)
    if not rows:
        raise SystemExit("no parseable rows -- is this a Windmill widget export?")

    by_pin = {}
    for ts, pin, val in rows:
        by_pin.setdefault(pin, 0)
        by_pin[pin] += 1
    t0, t1 = rows[0][0], rows[-1][0]

    replaced = con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(wh),0) wh FROM hourly WHERE unit=? "
        "AND hour>=? AND hour<=? AND COALESCE(estimated,0)=1",
        (unit, store.hour_of(t0), t1)).fetchone()

    info = {
        "unit": unit, "rows": len(rows), "pins": by_pin,
        "from": time.strftime("%Y-%m-%d %H:%M", time.localtime(t0)),
        "to": time.strftime("%Y-%m-%d %H:%M", time.localtime(t1)),
        "hours": (t1 - t0) / 3600,
        "replaces_estimated_hours": replaced["n"],
        "replaces_estimated_kwh": round((replaced["wh"] or 0) / 1000, 2),
        "unknown_columns": sorted(unknown),
    }
    if dry_run:
        return info

    # group by timestamp so write_sample's change detection works as it does live
    grouped = {}
    for ts, pin, val in rows:
        grouped.setdefault(ts, {})[pin] = val
    written = 0
    for ts in sorted(grouped):
        written += store.write_sample(con, ts, {unit: grouped[ts]})

    hours = sorted({store.hour_of(ts) for ts in grouped})
    store.rebuild_hours(con, hours, config.POWER_PIN,
                        energy.COMPRESSOR_W, energy.MAX_GAP)
    info["readings_written"] = written
    info["hours_rebuilt"] = len(hours)
    return info


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--unit", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    path = pathlib.Path(args.path).expanduser()
    if not path.exists():
        raise SystemExit(f"no such file: {path}")
    info = import_file(store.connect(), args.unit, path, args.dry_run)
    for k, v in info.items():
        print(f"  {k:<26} {v}")


if __name__ == "__main__":
    main()
