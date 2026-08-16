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


#: Estimate quality, best first. Real data outranks all of them.
RANK = {"runtime": 3, "idle": 2, "cohort": 1}


def write(con, rows):
    """Persist estimated hours.

    Never touches an hour holding real data, and never downgrades an existing
    estimate to a weaker method -- so a cohort guess cannot overwrite a
    reconstruction derived from the unit's own runtime counter.
    """
    keep = []
    for r in rows:
        cur = con.execute(
            "SELECT samples, estimated, method FROM hourly WHERE unit=? AND hour=?",
            (r[0], r[1])).fetchone()
        if cur and (cur["samples"] or not cur["estimated"]):
            continue                    # measured, or already replaced by a real fetch
        if cur and RANK.get(r[9], 0) < RANK.get(cur["method"], 0):
            continue                    # would be a downgrade
        keep.append(r)
    if keep:
        con.executemany(
            "INSERT OR REPLACE INTO hourly"
            "(unit,hour,wh,cooling_s,on_s,w_avg,w_max,samples,estimated,method) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", keep)
        con.commit()
    return len(keep)


def idle_draw(con, unit, default=25.0):
    """Median Wh/h for this unit's non-cooling hours -- what standby costs."""
    rows = [r["wh"] for r in con.execute(
        "SELECT wh FROM hourly WHERE unit=? AND COALESCE(estimated,0)=0 "
        "AND wh IS NOT NULL AND wh <= 100 ORDER BY wh", (unit,))]
    if not rows:
        return default
    return rows[len(rows) // 2]


def cohort_fill(con, unit, dry_run=False, window_days=3, exclude=()):
    """Last resort: no runtime counter at all, so borrow from units installed with it.

    Used when the export API cannot be reached (quota) and the unit has no
    history of its own to derive a rate from. This is the weakest estimate in
    the tool -- the magnitude comes entirely from other units -- so it is
    marked `cohort` and is the first thing replaced when real data arrives.
    """
    activated = store.activations(con)
    mine = activated.get(unit)
    if not mine:
        return {"unit": unit, "status": "no activation date", "hours": 0, "kwh": 0.0}
    peers = [u for u, a in activated.items()
             if u != unit and abs(a - mine) <= window_days * 86400]
    if not peers:
        peers = [d["name"] for d in config.DEVICES if d["name"] != unit]
    if not peers:
        return {"unit": unit, "status": "no peer units", "hours": 0, "kwh": 0.0}

    # anything already measured or estimated by a stronger method is left alone
    have = {r["hour"] for r in con.execute(
        "SELECT hour FROM hourly WHERE unit=? AND "
        "(COALESCE(estimated,0)=0 OR COALESCE(method,'runtime')<>'cohort')", (unit,))}
    have |= set(exclude)
    marks = ",".join("?" * len(peers))
    peer_rows = con.execute(
        f"SELECT hour, AVG(wh) wh FROM hourly WHERE unit IN ({marks}) "
        f"AND COALESCE(estimated,0)=0 GROUP BY hour", peers).fetchall()
    rows = []
    for r in peer_rows:
        h = r["hour"]
        if h in have or h < mine:
            continue
        wh = r["wh"] or 0.0
        rows.append((unit, h, wh, 3600.0 if wh > 100 else 0.0,
                     3600.0 if wh > 5 else 0.0, wh, wh, 0, 1, "cohort"))
    if not rows:
        return {"unit": unit, "status": "no gaps", "hours": 0, "kwh": 0.0}
    written = len(rows) if dry_run else write(con, rows)
    return {"unit": unit, "status": "would fill" if dry_run else "filled",
            "method": "cohort", "peers": peers, "hours": written,
            "kwh": round(sum(r[2] for r in rows) / 1000, 2)}


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

    idle_w = idle_draw(con, unit)
    rows, total_wh, total_run, spans = [], 0.0, 0.0, []
    methods = {}
    for start, end, ran in gaps:
        target = [h for h in missing if start < h < end]
        if not target:
            continue
        if ran > 0:
            # the unit ran: magnitude from its own counter, shape from the reference
            seg_wh = ran * wh_per_hour
            weight = sum(ref[h] for h in target)
            for h in target:
                share = (ref[h] / weight) if weight else 1 / len(target)
                wh = seg_wh * share
                secs = ran * 3600 * share
                rows.append((unit, h, wh, secs, secs, wh, wh, 0, 1, "runtime"))
            methods["runtime"] = methods.get("runtime", 0) + len(target)
            total_run += ran
        else:
            # the counter says it never ran, so this is standby draw, not zero
            seg_wh = idle_w * len(target)
            for h in target:
                rows.append((unit, h, idle_w, 0.0, 0.0, idle_w, idle_w, 0, 1, "idle"))
            methods["idle"] = methods.get("idle", 0) + len(target)
        total_wh += seg_wh
        spans.append((min(target), max(target)))

    if not rows:
        return {"unit": unit, "status": "no fillable gaps", "hours": 0, "kwh": 0.0}

    written = len(rows) if dry_run else write(con, rows)

    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
    return {"unit": unit, "reference": reference or "house average",
            "methods": methods, "idle_w": round(idle_w),
            "touched": [r[1] for r in rows], "written": written,
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
