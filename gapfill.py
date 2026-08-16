#!/usr/bin/env python3
"""Reconstruct energy for hours a unit ran but failed to report.

    python3 gapfill.py "Third Floor Right" --reference "Third Floor Left"

A unit can drop off Windmill's cloud for days while still cooling the room.
Those hours are genuinely missing energy, not zero, and leaving them at zero
understates that unit and every total containing it.

The reconstruction uses two independent things and refuses to guess without
the first:

  magnitude  the unit's own Filter Runtime (datastream 9) is cumulative and
             keeps counting while offline, so the difference across the gap is
             exactly how many hours it ran. Multiplied by the unit's own
             measured Wh-per-running-hour, that is its energy -- derived
             entirely from itself, not from a sibling.

  shape      a reference unit's hourly profile spreads that total across the
             gap, so the result has a plausible diurnal curve instead of a
             flat line. Only the shape is borrowed; the total does not depend
             on the reference at all.

Every hour written is marked `estimated = 1` and is reported separately by the
dashboard, so a reconstructed figure can never be mistaken for a measured one.
If Filter Runtime is unavailable, this refuses rather than falling back to
copying a sibling outright -- that would invent a magnitude, not just a shape.
"""
import argparse
import io
import json
import time
import urllib.request
import zipfile

import blynk
import config
import store

RUNTIME_DSID = 9        # "Filter Runtime", cumulative hours
POWER_PIN_RUNTIME = "v7"


def runtime_history(device):
    """[(ts, cumulative running hours)] from the export API."""
    url = (f"https://{config.HOST}/external/api/data/get?token={device['token']}"
           f"&period=month&granularityType=HOURLY&dataStreamId={RUNTIME_DSID}")
    with urllib.request.urlopen(url, timeout=30) as r:
        payload = json.load(r)
    if "link" not in payload:
        raise RuntimeError(payload.get("error", {}).get("message", "no export link"))
    with urllib.request.urlopen(payload["link"], timeout=90) as r:
        z = zipfile.ZipFile(io.BytesIO(r.read()))
    name = next(n for n in z.namelist() if n.endswith("_data.csv"))
    lines = z.read(name).decode().strip().splitlines()
    if "Filter Runtime" not in lines[0]:
        raise RuntimeError(f"datastream {RUNTIME_DSID} is {lines[0].split(',',1)[1]!r}, "
                           f"not Filter Runtime")
    return sorted((int(l.split(",")[0]), float(l.split(",")[1])) for l in lines[1:])


def reference_shape(con, unit, reference=None):
    """{hour: wh} used only to spread a known total across a gap.

    A named reference unit is used when given. Otherwise the mean across every
    other unit that reported in that hour, which covers gaps no single sibling
    spans.
    """
    if reference:
        return {r["hour"]: (r["wh"] or 0) for r in con.execute(
            "SELECT hour, wh FROM hourly WHERE unit=?", (reference,))}
    return {r["hour"]: (r["wh"] or 0) for r in con.execute(
        "SELECT hour, AVG(wh) wh FROM hourly WHERE unit<>? GROUP BY hour", (unit,))}


def find_gaps(con, unit, reference=None):
    """Hours the reference covers, after this unit existed, that it lacks."""
    have = {r["hour"] for r in con.execute(
        "SELECT hour FROM hourly WHERE unit=?", (unit,))}
    ref = reference_shape(con, unit, reference)
    activated = store.activations(con).get(unit)
    return sorted(h for h in ref
                  if h not in have and (not activated or h >= activated)), ref


def segment(run, now_ts, now_runtime, gap_after=2 * 3600):
    """Split the runtime series into reported stretches and reporting gaps.

    Returns (reported_runtime_hours, [(start_ts, end_ts, runtime_hours), ...]).
    A gap is any pair of consecutive samples more than `gap_after` apart; the
    counter is cumulative, so the difference across it is exactly how long the
    unit ran while silent. A trailing gap up to `now` is included so an outage
    still in progress is not missed.
    """
    reported, gaps = 0.0, []
    for (t0, v0), (t1, v1) in zip(run, run[1:]):
        delta = max(0.0, v1 - v0)
        if t1 - t0 > gap_after:
            gaps.append((t0, t1, delta))
        else:
            reported += delta
    if now_runtime and now_ts - run[-1][0] > gap_after:
        gaps.append((run[-1][0], now_ts, max(0.0, now_runtime - run[-1][1])))
    return reported, gaps


def fill(con, unit, reference=None, dry_run=False):
    device = next((d for d in config.DEVICES if d["name"] == unit), None)
    if not device:
        raise SystemExit(f"unknown unit: {unit}")
    if reference and not any(d["name"] == reference for d in config.DEVICES):
        raise SystemExit(f"unknown reference: {reference}")

    missing, ref = find_gaps(con, unit, reference)
    if not missing:
        return {"unit": unit, "status": "no gaps", "hours": 0}

    run = runtime_history(device)
    live = blynk.read(device) or {}
    now_runtime = blynk.num(live.get(POWER_PIN_RUNTIME))
    if len(run) < 2:
        return {"unit": unit, "status": "no runtime counter", "hours": 0}

    reported_run, gaps = segment(run, int(time.time()), now_runtime)
    if not gaps:
        return {"unit": unit, "status": "no reporting gaps in runtime counter",
                "hours": 0}

    # Wh per running hour, from this unit's own measured hours only -- runtime
    # accrued while silent is excluded, or the rate would be biased downward.
    measured = con.execute(
        "SELECT SUM(wh) s FROM hourly WHERE unit=? AND hour>=? AND hour<=? "
        "AND COALESCE(estimated,0)=0",
        (unit, run[0][0], run[-1][0])).fetchone()["s"] or 0
    if reported_run <= 0 or measured <= 0:
        return {"unit": unit, "status": "cannot derive Wh/running-hour", "hours": 0}
    wh_per_hour = measured / reported_run

    rows, total_wh, total_run, spans = [], 0.0, 0.0, []
    for start, end, ran in gaps:
        target = [h for h in missing if start < h < end]
        if not target or ran <= 0:
            continue
        seg_wh = ran * wh_per_hour
        weight = sum(ref[h] for h in target)
        for h in target:
            share = (ref[h] / weight) if weight else 1 / len(target)
            wh = seg_wh * share
            secs = ran * 3600 * share
            rows.append((unit, h, wh, secs, secs, wh, wh, 0, 1))
        total_wh += seg_wh
        total_run += ran
        spans.append((min(target), max(target)))

    if not rows:
        return {"unit": unit, "status": "unit did not run during the gaps",
                "hours": len(missing), "kwh": 0.0}

    if not dry_run:
        con.executemany(
            "INSERT OR REPLACE INTO hourly"
            "(unit,hour,wh,cooling_s,on_s,w_avg,w_max,samples,estimated) "
            "VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.commit()

    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
    return {"unit": unit, "reference": reference or "house average",
            "status": "would fill" if dry_run else "filled",
            "hours": len(rows), "kwh": round(total_wh / 1000, 2),
            "gap_runtime_h": round(total_run, 1),
            "wh_per_running_hour": round(wh_per_hour),
            "spans": ", ".join(f"{fmt(a)} -> {fmt(b)}" for a, b in spans)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("unit")
    p.add_argument("--reference",
                   help="comparable unit whose hourly shape spreads the total "
                        "(default: mean of all other units)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    r = fill(store.connect(), args.unit, args.reference, args.dry_run)
    for k, v in r.items():
        print(f"  {k:<20} {v}")


if __name__ == "__main__":
    main()
