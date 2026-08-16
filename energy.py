"""Energy math and range queries.

kWh is integrated locally from sampled watts rather than read from a cloud
counter. Two resolutions feed it:

  * locally sampled `readings` -- full fidelity, only for time this tool has
    been running
  * the `hourly` rollup -- built from those readings, and seeded further back
    by Windmill's own hourly export (see backfill.py)

Short ranges chart from raw readings; long ranges chart from the rollup, so a
year-wide query touches a few thousand rows instead of millions.
"""
import time
import calendar

import analysis
import blynk
import config
import store

COMPRESSOR_W = 100    # sustained draw above this means the compressor is running
MAX_GAP = 900         # never integrate more than this across a hole in sampling
RAW_SPAN = 6 * 3600   # ranges at or below this chart from raw readings


# ------------------------------------------------------------------ ranges

def day_start(ts=None):
    lt = time.localtime(ts if ts is not None else time.time())
    return time.mktime(lt[:3] + (0, 0, 0, 0, 0, -1))


def resolve_range(name, now=None):
    """Named preset -> (t0, t1, label). Everything is local-time aligned."""
    now = now or time.time()
    today = day_start(now)
    lt = time.localtime(now)
    presets = {
        "today":  (today, now, "Today"),
        "24h":    (now - 86400, now, "Last 24 hours"),
        "7d":     (today - 6 * 86400, now, "Last 7 days"),
        "30d":    (today - 29 * 86400, now, "Last 30 days"),
        "mtd":    (time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)), now,
                   "Month to date"),
        "ytd":    (time.mktime((lt.tm_year, 1, 1, 0, 0, 0, 0, 0, -1)), now,
                   "Year to date"),
        "12mo":   (now - 365 * 86400, now, "Last 12 months"),
    }
    if name in presets:
        return presets[name]
    return presets["today"]


def bucket_for(span):
    """Chart bucket width that keeps a range to a readable number of points."""
    if span <= 2 * 86400:
        return 3600
    if span <= 31 * 86400:
        return 6 * 3600
    if span <= 120 * 86400:
        return 86400
    return 7 * 86400


# ------------------------------------------------------------- integration

def integrate_points(points, t_end=None):
    """(kWh, cooling_s, on_s) from [(ts, watts)] using the trapezoid rule."""
    wh = cooling = 0.0
    prev = None
    for ts, watts in points:
        if prev is not None:
            dt = ts - prev[0]
            if 0 < dt <= MAX_GAP:
                mean = (watts + prev[1]) / 2
                wh += mean * dt / 3600
                if mean > COMPRESSOR_W:
                    cooling += dt
        prev = (ts, watts)
    if prev and t_end and 0 < t_end - prev[0] <= MAX_GAP:
        dt = t_end - prev[0]
        wh += prev[1] * dt / 3600
        if prev[1] > COMPRESSOR_W:
            cooling += dt
    return wh / 1000, cooling, 0.0


# ---------------------------------------------------------------- assembly

def rate(con):
    return float(store.get_meta(con, "rate_per_kwh", config.RATE_PER_KWH))


def summary(con, t0, t1, label="", live=None):
    span = max(1, t1 - t0)
    use_raw = span <= RAW_SPAN
    r = rate(con)

    totals_by_unit = {} if use_raw else store.rollup_range(con, t0, t1)
    units, total_kwh, total_w = [], 0.0, 0.0
    activated = store.activations(con)
    now = time.time()

    for device in config.DEVICES:
        name = device["name"]
        pins = (live or {}).get(name)
        state = blynk.describe(pins)

        # A unit only owes us data for the part of the range it existed for, so
        # a recently installed unit is not mistaken for one with missing history.
        start = max(t0, activated.get(name, t0))
        expected = max(0.0, (min(t1, now) - start) / 3600)
        covered = store.covered_hours(con, name, start, t1)
        ratio = (covered / expected) if expected >= 1 else 1.0
        complete = ratio >= 0.5

        if use_raw:
            pts = store.series(con, name, config.POWER_PIN, t0, t1)
            kwh, cooling, _ = integrate_points(pts, t_end=min(t1, time.time()))
            peak = max([v for _, v in pts], default=0)
            est_kwh = 0.0
        else:
            row = totals_by_unit.get(name, {})
            kwh = (row.get("wh") or 0) / 1000
            cooling = row.get("cooling_s") or 0
            peak = row.get("w_max") or 0
            est_kwh = (row.get("est_wh") or 0) / 1000

        total_w += state["watts"]
        if complete:
            total_kwh += kwh
        units.append({
            **state,
            "name": name,
            "watts": round(state["watts"]),
            "kwh": round(kwh, 3) if complete else None,
            "cost": round(kwh * r, 2) if complete else None,
            "cooling_min": round(cooling / 60) if complete else None,
            "peak_w": round(peak),
            "complete": complete,
            "est_kwh": round(est_kwh, 3),
            "coverage": round(ratio, 3),
            "missing_h": round(max(0.0, expected - covered)),
            "pins": pins or {},
        })

    for u in units:
        u["share"] = (round(100 * u["kwh"] / total_kwh, 1)
                      if (total_kwh and u["complete"]) else None)

    # ---- chart series
    bucket = bucket_for(span)
    if use_raw:
        series_points = {}
        stamps = set()
        for device in config.DEVICES:
            pts = store.series(con, device["name"], config.POWER_PIN, t0, t1)
            series_points[device["name"]] = dict(pts)
            stamps.update(p[0] for p in pts)
        stamps = sorted(stamps)
        series = {
            "t": stamps,
            "unit": "W",
            "bucket": 0,
            "units": {n: [series_points[n].get(s) for s in stamps] for n in series_points},
        }
        # carry each unit's last known level forward across timestamps it didn't report
        for n, vals in series["units"].items():
            last = None
            for i, v in enumerate(vals):
                if v is None:
                    vals[i] = last
                else:
                    last = v
    else:
        buckets = store.rollup_buckets(con, t0, t1, bucket)
        stamps = [b for b, _ in buckets]
        series = {
            "t": stamps,
            "unit": "kWh",
            "bucket": bucket,
            "units": {
                d["name"]: [round((vals.get(d["name"], 0) or 0) / 1000, 4)
                            for _, vals in buckets]
                for d in config.DEVICES
            },
        }

    # ---- temperature series, bucketed onto the same axis as the energy chart
    tb = bucket or 3600
    analysis.ensure_hourly_weather(con, t0, t1, timeout=8)
    outdoor_raw = analysis.outdoor_series(con, t0, t1)
    indoor_raw = {}
    for row in con.execute(
            "SELECT hour, AVG(temp_f) t FROM hourly WHERE hour>=? AND hour<? "
            "AND temp_f IS NOT NULL GROUP BY hour", (store.hour_of(t0), t1)):
        indoor_raw[row["hour"]] = row["t"]

    def bucketize(raw):
        acc = {}
        for h, v in raw.items():
            b = int(h) // tb * tb
            acc.setdefault(b, []).append(v)
        return {b: sum(v) / len(v) for b, v in acc.items()}

    ind, out = bucketize(indoor_raw), bucketize(outdoor_raw)
    tstamps = sorted(set(ind) | set(out))
    temps = {
        "t": tstamps,
        "indoor": [round(ind[b], 1) if b in ind else None for b in tstamps],
        "outdoor": [round(out[b], 1) if b in out else None for b in tstamps],
        "bucket": tb,
    }

    lo, hi = store.bounds(con)
    hourly_lo = con.execute("SELECT MIN(hour) h FROM hourly").fetchone()["h"]
    coverage_start = min([x for x in (lo, hourly_lo) if x], default=None)

    return {
        "updated": int(time.time()),
        "range": {"from": int(t0), "to": int(t1), "label": label, "span": int(span)},
        "rate": r,
        "units": units,
        "totals": {
            "watts": round(total_w),
            "kwh": round(total_kwh, 3),
            "cost": round(total_kwh * r, 2),
            "cooling_min": sum(u["cooling_min"] or 0 for u in units),
            "online": sum(1 for u in units if u["online"]),
            "count": len(units),
            "daily_avg_kwh": round(total_kwh / max(1, span / 86400), 3),
            "incomplete": [u["name"] for u in units if not u["complete"]],
            "est_kwh": round(sum(u["est_kwh"] for u in units if u["complete"]), 3),
            "estimated_units": [u["name"] for u in units
                                if u["complete"] and u["est_kwh"] > 0.01],
        },
        "series": series,
        "temps": temps,
        "coverage": {"start": coverage_start, "end": hi},
        "resolution": "raw samples" if use_raw else "hourly rollup",
    }


def snapshot(con, t0, t1, label="", with_live=True):
    live = blynk.read_all() if with_live else {}
    return summary(con, t0, t1, label, live)
