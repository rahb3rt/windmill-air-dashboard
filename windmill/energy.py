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

from . import analysis, blynk, config, overrun, store


COMPRESSOR_W = 100    # sustained draw above this means the compressor is running
MAX_GAP = 900         # never integrate more than this across a hole in sampling
RAW_SPAN = 6 * 3600   # ranges at or below this chart from raw readings


# ------------------------------------------------------------------ ranges

def day_start(ts=None):
    lt = time.localtime(ts if ts is not None else time.time())
    return time.mktime(lt[:3] + (0, 0, 0, 0, 0, -1))


#: Accepted `range=` values, in the order they are offered. Named here so the
#: API docs can list them without restating what resolve_range already knows.
PRESET_NAMES = ("today", "6h", "24h", "7d", "30d", "mtd", "ytd", "12mo")


def resolve_range(name, now=None):
    """Named preset -> (t0, t1, label). Everything is local-time aligned."""
    now = now or time.time()
    today = day_start(now)
    lt = time.localtime(now)
    presets = {
        "today":  (today, now, "Today"),
        "6h":     (now - RAW_SPAN, now, "Last 6 hours"),
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
    """(kWh, cooling_s, on_s) from [(ts, watts)], holding each value forward.

    Readings are written on change and held until the next one -- `store.series`
    says so, and reconstructs them that way. So the value between two samples is
    the *earlier* one, and the area under it is a rectangle. The trapezoid rule
    used here previously drew a ramp between samples that the unit never
    reported, and the error grew with the gap: idle units, whose gaps are long,
    read up to 30% high against their own meters.

    A gap longer than MAX_GAP is clamped, not discarded. Dropping it -- the old
    behaviour -- silently lost real energy every time a keyframe landed a poll
    late, since a keyframe every 900s against a 900s limit is a coin flip. That
    is the opposite-direction error that made busy units read low, and it also
    put this out of step with `store.rebuild_hours`, which always clamped.

    Checked against `v16`, the units' own energy counters: worst per-unit error
    30.0% -> 5.5%, and every unit except one within 1%.
    """
    wh = cooling = 0.0
    prev = None
    for ts, watts in points:
        if prev is not None:
            dt = min(ts - prev[0], MAX_GAP)
            if dt > 0:
                wh += prev[1] * dt / 3600
                if prev[1] > COMPRESSOR_W:
                    cooling += dt
        prev = (ts, watts)
    if prev and t_end and t_end > prev[0]:
        dt = min(t_end - prev[0], MAX_GAP)
        wh += prev[1] * dt / 3600
        if prev[1] > COMPRESSOR_W:
            cooling += dt
    return wh / 1000, cooling, 0.0


# ------------------------------------------------------------ room naming

def room_label(unit, t0, t1):
    """What room this device was actually in, for the range being viewed.

    Device names track where a unit is *now*. When units are moved, their older
    history belongs to a different room, and labelling it with today's name
    silently misattributes energy. Returns (display, prior) -- `prior` is set
    only when the range straddles the move, where no single name is correct.
    """
    prior = config.PRIOR_ROOMS.get(unit)
    if not prior or not config.MOVED_ON:
        return unit, None
    moved = time.mktime(time.strptime(config.MOVED_ON, "%Y-%m-%d"))
    if t1 <= moved:
        return prior, None                 # entirely before the move
    if t0 >= moved:
        return unit, None                  # entirely after
    return unit, prior                     # spans it


# ---------------------------------------------------------------- assembly

def rate(con):
    return float(store.get_meta(con, "rate_per_kwh", config.RATE_PER_KWH))


#: Connected, but no heartbeat for this long, means the unit has stopped
#: talking even though the cloud still answers for it. Must match
#: serve.WEDGED_S -- kept here so the page can say so without a second request,
#: and the two disagreeing would have the card contradict the watchdog.
WEDGED_S = 3600


def sync_health(con, name, state, now):
    """Per-unit sync verdict, from the link pin plus how long the unit has been
    quiet. Costs no extra API calls -- both facts are already in hand."""
    changed = store.last_change(con, name, blynk.HEARTBEAT, now=now)
    quiet = None if changed is None else int(now - changed)
    if state["linked"] is False:
        sync = "dropped"
    elif quiet is not None and quiet > WEDGED_S:
        sync = "wedged"
    elif state["linked"] is None:
        sync = "unknown"
    else:
        sync = "ok"
    saved = store.get_settings(con, name)
    return {"sync": sync, "quiet_s": quiet, "reported_at": changed,
            "saved": blynk.settings_label(saved) if saved else None,
            "saved_at": store.settings_meta(con, name).get("updated_at")}


def overrun_health(con, now):
    """{unit: verdict} for cooling-with-no-demand, computed once per request."""
    try:
        return overrun.report(con, int(now))
    except Exception:
        return {}          # a diagnostic must never take the dashboard down


#: Points per series in a unit detail view. Enough to keep every compressor
#: cycle visible at a day's width without shipping a week of 30s samples.
DETAIL_POINTS = 900


def _thin(points, limit=DETAIL_POINTS):
    """Drop points evenly, always keeping the first and last.

    Values are carried forward between samples, so dropping intermediate ones
    changes where a step is drawn but never invents a level that was not held.
    """
    if len(points) <= limit:
        return [[int(ts), v] for ts, v in points]
    step = len(points) / limit
    kept = [points[int(i * step)] for i in range(limit)]
    if kept[-1] != points[-1]:
        kept.append(points[-1])
    return [[int(ts), v] for ts, v in kept]


#: Names for the datastreams that have been identified. Everything a unit
#: reports is shown either way -- an unnamed pin is still evidence, and hiding
#: it would mean the "everything it reports" view quietly wasn't.
PIN_NAMES = {
    "v0": "Power switch", "v1": "Room temperature", "v2": "Set temperature",
    "v3": "Mode", "v4": "Fan speed", "v7": "Filter runtime",
    "v11": "Filter change indicator", "v15": "Power draw",
    "v16": "Energy, last 15 min", "v30": "WiFi signal",
    "v31": "Protocol failure count", blynk.LINK: "Cloud session",
}
PIN_UNITS = {"v1": "°F", "v2": "°F", "v7": "h", "v15": "W", "v16": "kWh",
             "v30": "dBm"}


def live_view(con, name, pins, now):
    """Everything currently known about one unit, for its detail panel."""
    state = blynk.describe(pins)
    rows = []
    for pin, raw in sorted((pins or {}).items(),
                           key=lambda kv: (kv[0][0] != "v", _pin_no(kv[0]))):
        val = blynk.num(raw, None)
        rows.append({
            "pin": pin,
            "name": PIN_NAMES.get(pin),
            "unit": PIN_UNITS.get(pin, ""),
            "value": raw,
            "shown": _pin_text(pin, val),
        })
    return {
        "state": state,
        "pins": rows,
        "named": sum(1 for r in rows if r["name"]),
        **sync_health(con, name, state, now),
    }


def _pin_no(pin):
    try:
        return int(pin[1:])
    except ValueError:
        return 9999


def _pin_text(pin, val):
    """A pin's value in words where words exist for it."""
    if val is None:
        return "–"
    if pin == "v0":
        return "On" if val == 1 else "Off"
    if pin == "v3":
        return blynk.MODES.get(int(val), str(val))
    if pin == "v4":
        return blynk.FANS.get(int(val), str(val))
    if pin == "v11":
        return "Change the filter" if val == 1 else "Filter fine"
    if pin == blynk.LINK:
        return "Connected" if val == 1 else "Dropped"
    return f"{val:g}"


def unit_detail(con, name, t0, t1, label="", live=None):
    """Everything one unit reported over a range, for its own chart.

    Failures are returned as the *increments* rather than the raw counter: the
    counter is cumulative and monotonic, so plotting it draws a line that only
    ever goes up and says nothing about when the unit was actually struggling.
    """
    pins = {"watts": config.POWER_PIN, "temp": blynk.TEMP, "target": blynk.TARGET,
            "power": blynk.POWER, "rssi": "v30", "link": blynk.LINK}
    out = {k: _thin(store.series(con, name, p, t0, t1)) for k, p in pins.items()}

    # Local history is younger than most ranges, so a 30-day view of a unit
    # logged for eight days would spend three quarters of its width empty.
    # Start the axis where the data does, and say so rather than implying the
    # range asked for was covered.
    starts = [v[0][0] for v in out.values() if v]
    first = min(starts) if starts else None
    trimmed = bool(first and first > t0 + 60)
    plotted = first if trimmed else int(t0)

    raw = store.series(con, name, blynk.FAILURES, t0, t1)
    events, prev = [], None
    for ts, v in raw:
        if prev is not None and v > prev:
            events.append([int(ts), int(v - prev), int(v)])
        prev = v
    out["failures"] = events

    kwh, cooling, _ = integrate_points(
        store.series(con, name, config.POWER_PIN, t0, t1), t_end=min(t1, time.time()))
    display, prior = room_label(name, t0, t1)
    floors = store.get_floors(con)
    saved = store.get_settings(con, name)
    return {
        "unit": name,
        "display": display,
        "prior_room": prior,
        "floor": floors.get(name),
        "saved": blynk.settings_label(saved) if saved else None,
        "saved_at": store.settings_meta(con, name).get("updated_at"),
        "live": live_view(con, name, live, time.time()) if live is not None else None,
        "range": {"from": int(plotted), "to": int(t1), "label": label,
                  "requested_from": int(t0), "trimmed": trimmed},
        "series": out,
        "totals": {
            "kwh": round(kwh, 3),
            "cost": round(kwh * rate(con), 2),
            "cooling_min": round(cooling / 60),
            "failures": sum(e[1] for e in events),
            "peak_w": round(max([v for _, v in store.series(
                con, name, config.POWER_PIN, t0, t1)], default=0)),
        },
    }


def summary(con, t0, t1, label="", live=None):
    span = max(1, t1 - t0)
    use_raw = span <= RAW_SPAN
    r = rate(con)

    totals_by_unit = {} if use_raw else store.rollup_range(con, t0, t1)
    units, total_kwh, total_w = [], 0.0, 0.0
    activated = store.activations(con)
    floors = store.get_floors(con)
    labels = store.labels(con)
    now = time.time()
    overruns = overrun_health(con, now)

    for device in config.DEVICES:
        name = device["name"]
        pins = (live or {}).get(name)
        state = blynk.describe(pins)

        # A unit only owes us data for the part of the range it existed for, so
        # a recently installed unit is not mistaken for one with missing history.
        # a unit that did not exist yet is not part of this range at all --
        # listing it would also collide with whichever device then held its room
        installed_at = activated.get(name)
        installed = not installed_at or installed_at < t1

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
            est_methods = ""
        else:
            row = totals_by_unit.get(name, {})
            kwh = (row.get("wh") or 0) / 1000
            cooling = row.get("cooling_s") or 0
            peak = row.get("w_max") or 0
            est_kwh = (row.get("est_wh") or 0) / 1000
            est_methods = row.get("methods") or ""

        # a desynced unit's last reported watts are not what it is drawing now
        total_w += state["watts"] if installed and not state["stale"] else 0
        if complete and installed:
            total_kwh += kwh
        display, prior = room_label(name, t0, t1)
        # A label set in Settings is what a person calls this unit, so it wins
        # over the device name -- except where the room-move logic has something
        # more truthful to say about an older range.
        if prior is None and labels.get(name):
            display = labels[name]
        units.append({
            **state,
            "name": name,
            "display": display,
            "floor": floors.get(name),
            "prior_room": prior,
            "installed": installed,
            "watts": round(state["watts"]),
            "kwh": round(kwh, 3) if complete else None,
            "cost": round(kwh * r, 2) if complete else None,
            "cooling_min": round(cooling / 60) if complete else None,
            "peak_w": round(peak),
            "complete": complete,
            "est_kwh": round(est_kwh, 3),
            "est_share": round(100 * est_kwh / kwh, 1) if kwh > 0 else 0.0,
            "est_methods": sorted(m for m in est_methods.split(",") if m),
            "coverage": round(ratio, 3),
            "missing_h": round(max(0.0, expected - covered)),
            "pins": pins or {},
            **sync_health(con, name, state, now),
            "overrun": (overruns.get(name) or {}).get("verdict"),
            "overrun_detail": (overruns.get(name) or {}).get("detail"),
            "duty": (overruns.get(name) or {}).get("duty"),
            "floor_duty": (overruns.get(name) or {}).get("floor_duty"),
        })

    units = [u for u in units if u["installed"]]
    for u in units:
        u["share"] = (round(100 * u["kwh"] / total_kwh, 1)
                      if (total_kwh and u["complete"]) else None)

    # ---- chart series
    bucket = bucket_for(span)
    if use_raw:
        series_points = {}
        stamps = set()
        for u in units:
            pts = store.series(con, u["name"], config.POWER_PIN, t0, t1)
            series_points[u["name"]] = dict(pts)
            stamps.update(p[0] for p in pts)
        stamps = sorted(stamps)
        series = {
            "t": stamps,
            "unit": "W",
            "bucket": 0,
            "units": {n: [series_points[n].get(s) for s in stamps] for n in series_points},
        }
        # Carry each unit's last known level forward across timestamps it didn't
        # report -- but only while it still had a session. Carrying across a
        # desync draws a flat line that reads as a genuinely steady load; a
        # break in the line is the truthful shape for time we heard nothing.
        for n, vals in series["units"].items():
            live = store.step_fn(store.series(con, n, blynk.LINK, t0, t1), default=1)
            last = None
            for i, v in enumerate(vals):
                if not live(stamps[i]):
                    vals[i], last = None, None
                elif v is None:
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
                u["name"]: [round((vals.get(u["name"], 0) or 0) / 1000, 4)
                            for _, vals in buckets]
                for u in units
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
            "desynced": [u["display"] or u["name"] for u in units if u["stale"]],
            "floors": sorted({u["floor"] for u in units if u["floor"]}),
            "daily_avg_kwh": round(total_kwh / max(1, span / 86400), 3),
            "incomplete": [u["name"] for u in units if not u["complete"]],
            "est_kwh": round(sum(u["est_kwh"] for u in units if u["complete"]), 3),
            "estimated_units": [u["name"] for u in units
                                if u["complete"] and u["est_kwh"] > 0.01],
        },
        "series": series,
        "temps": temps,
        "moved_on": config.MOVED_ON if any(u["prior_room"] for u in units)
                    or any(u["display"] != u["name"] for u in units) else None,
        "coverage": {"start": coverage_start, "end": hi},
        "resolution": "raw samples" if use_raw else "hourly rollup",
    }


def snapshot(con, t0, t1, label="", with_live=True):
    live = blynk.read_all() if with_live else {}
    return summary(con, t0, t1, label, live)
