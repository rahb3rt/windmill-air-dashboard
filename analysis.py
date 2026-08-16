"""Weather-normalized before/after comparison of a change to the system.

Comparing raw kWh either side of an install mostly measures the weather. This
regresses daily household kWh on cooling degree days (daily mean above 65F) for
each period, then asks at what temperature the two configurations cost the same.

Two honesty rules are built in rather than left to the reader:

  * A unit whose history barely covers the post-change period is not silently
    counted as zero. It is imputed from its siblings, and every figure it
    touches is reported as a low-high band.
  * The break-even point is usually an extrapolation beyond the temperatures
    actually observed. `extrapolated` says so, and the UI is expected to show it.

Weather comes from Open-Meteo and is cached in SQLite forever -- it is history,
it does not change, and the API is slow enough that re-fetching is painful.
"""
import json
import statistics
import time
import urllib.parse
import urllib.request

import config
import store

BASE_F = 65.0
SUMMER = ("06", "07", "08", "09")


# ---------------------------------------------------------------- weather

def _fetch_weather(start, end, timeout=45):
    q = urllib.parse.urlencode({
        "latitude": config.LAT, "longitude": config.LON,
        "start_date": start, "end_date": end,
        "daily": "temperature_2m_mean,temperature_2m_max",
        "temperature_unit": "fahrenheit", "timezone": "auto",
    })
    url = f"https://archive-api.open-meteo.com/v1/archive?{q}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        d = json.load(r)["daily"]
    return [(day, m, x) for day, m, x in
            zip(d["time"], d["temperature_2m_mean"], d["temperature_2m_max"])
            if m is not None]


def ensure_weather(con, start, end, timeout=45):
    """Fill any gap in the cache for [start, end]. Degrades silently on failure."""
    have = {r["day"] for r in con.execute(
        "SELECT day FROM weather WHERE day>=? AND day<=?", (start, end))}
    want_days = (time.mktime(time.strptime(end, "%Y-%m-%d"))
                 - time.mktime(time.strptime(start, "%Y-%m-%d"))) / 86400 + 1
    if len(have) >= want_days - 1:          # today's row often lags; tolerate one
        return True
    try:
        rows = _fetch_weather(start, end, timeout)
    except Exception:
        return False
    con.executemany(
        "INSERT OR REPLACE INTO weather(day,mean_f,max_f) VALUES (?,?,?)", rows)
    con.commit()
    return True


def weather_map(con, start=None, end=None):
    sql = "SELECT day, mean_f, max_f FROM weather"
    args = ()
    if start and end:
        sql += " WHERE day>=? AND day<=?"
        args = (start, end)
    return {r["day"]: (r["mean_f"], r["max_f"]) for r in con.execute(sql, args)}


def ensure_hourly_weather(con, t0, t1, timeout=45):
    """Hourly outdoor temperature for [t0,t1], cached permanently."""
    start = time.strftime("%Y-%m-%d", time.localtime(t0))
    end = time.strftime("%Y-%m-%d", time.localtime(t1))
    want = int((t1 - t0) / 3600)
    have = con.execute("SELECT COUNT(*) n FROM weather_hourly WHERE hour>=? AND hour<=?",
                       (int(t0), int(t1))).fetchone()["n"]
    if have >= want - 24:
        return True
    q = urllib.parse.urlencode({
        "latitude": config.LAT, "longitude": config.LON,
        "start_date": start, "end_date": end,
        "hourly": "temperature_2m", "temperature_unit": "fahrenheit",
        "timezone": "auto",
    })
    try:
        with urllib.request.urlopen(
                f"https://archive-api.open-meteo.com/v1/archive?{q}", timeout=timeout) as r:
            d = json.load(r)["hourly"]
    except Exception:
        return False
    rows = []
    for iso, temp in zip(d["time"], d["temperature_2m"]):
        if temp is None:
            continue
        ts = int(time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M")))
        rows.append((ts, temp))
    con.executemany("INSERT OR REPLACE INTO weather_hourly(hour,temp_f) VALUES (?,?)", rows)
    con.commit()
    return True


def outdoor_series(con, t0, t1):
    return {r["hour"]: r["temp_f"] for r in con.execute(
        "SELECT hour, temp_f FROM weather_hourly WHERE hour>=? AND hour<=?",
        (int(t0), int(t1)))}


# ---------------------------------------------------------------- energy

def daily_totals(con):
    """({day: {unit: kWh}}, {day: estimated kWh}) from the hourly rollup."""
    days, est = {}, {}
    for r in con.execute(
            "SELECT unit, hour, wh, COALESCE(estimated,0) e FROM hourly ORDER BY hour"):
        key = time.strftime("%Y-%m-%d", time.localtime(r["hour"]))
        kwh = (r["wh"] or 0) / 1000
        days.setdefault(key, {})[r["unit"]] = \
            days.setdefault(key, {}).get(r["unit"], 0.0) + kwh
        if r["e"]:
            est[key] = est.get(key, 0.0) + kwh
    return days, est


def fit(points):
    n = len(points)
    if n < 3:
        return None
    sx = sum(x for x, _ in points); sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points); sxy = sum(x * y for x, y in points)
    den = n * sxx - sx * sx
    if not den:
        return None
    b = (n * sxy - sx * sy) / den
    a = (sy - b * sx) / n
    ybar = sy / n
    sst = sum((y - ybar) ** 2 for _, y in points)
    ssr = sum((y - (a + b * x)) ** 2 for x, y in points)
    return {"a": a, "b": b, "r2": (1 - ssr / sst) if sst else 0.0, "n": n,
            "cdd_lo": min(x for x, _ in points), "cdd_hi": max(x for x, _ in points)}


# ---------------------------------------------------------------- compare

def comparison(con, change=None, new_units=None, settle=2, rate=None):
    change = change or config.CHANGE_DATE
    new_units = set(new_units or config.NEW_UNITS)
    if not change or not new_units:
        return {"available": False,
                "reason": "Set WINDMILL_CHANGE_DATE and WINDMILL_NEW_UNITS in .env"}

    rate = rate if rate is not None else float(
        store.get_meta(con, "rate_per_kwh", config.RATE_PER_KWH))
    days, est_by_day = daily_totals(con)
    if not days:
        return {"available": False, "reason": "No history yet"}

    lo, hi = min(days), max(days)
    ensure_weather(con, lo, hi, timeout=20)
    wx = weather_map(con)

    resume = time.strftime("%Y-%m-%d", time.localtime(
        time.mktime(time.strptime(change, "%Y-%m-%d")) + (settle + 1) * 86400))
    partial = hi                       # last day is usually incomplete

    # --- coverage: a new unit missing from the post window must not read as zero
    post_days = [d for d in sorted(days) if d >= resume and d != partial]
    covered, missing = {}, []
    for u in new_units:
        seen = [days[d].get(u, 0.0) for d in post_days]
        n_present = sum(1 for d in post_days if u in days[d])
        if post_days and n_present >= max(1, len(post_days) * 0.5):
            covered[u] = sum(seen) / len(post_days)
        else:
            missing.append(u)
    band = (min(covered.values()), max(covered.values())) if covered else (0.0, 0.0)
    impute_lo = band[0] * len(missing)
    impute_hi = band[1] * len(missing)

    pre, post = [], []
    for day in sorted(days):
        if day not in wx or day == partial:
            continue
        cdd = max(0.0, wx[day][0] - BASE_F)
        total = sum(days[day].values())
        if day < change:
            pre.append((cdd, total))
        elif day >= resume:
            post.append((cdd, total))

    def est_share(day_list):
        tot = sum(sum(days[d].values()) for d in day_list)
        e = sum(est_by_day.get(d, 0.0) for d in day_list)
        return round(100 * e / tot, 1) if tot else 0.0

    pre_days = [d for d in sorted(days) if d < change and d in wx and d != partial]
    est_pre, est_post = est_share(pre_days), est_share(post_days)

    fp, fq = fit(pre), fit(post)
    if not (fp and fq):
        return {"available": False,
                "reason": f"Need more days either side of {change} "
                          f"({len(pre)} before, {len(post)} after)"}

    # delta(cdd) = (aq + k - ap) + (bq - bp) * cdd, for imputed load k
    d_slope = fq["b"] - fp["b"]
    def breakeven(k):
        base = fq["a"] + k - fp["a"]
        return (-base / d_slope) if d_slope < 0 and base > 0 else None

    be_lo, be_hi = breakeven(impute_lo), breakeven(impute_hi)

    rows = []
    for cdd in (5, 10, 15, 20):
        before = fp["a"] + fp["b"] * cdd
        rows.append({
            "mean_f": BASE_F + cdd,
            "before": round(before, 1),
            "after_lo": round(fq["a"] + impute_lo + fq["b"] * cdd, 1),
            "after_hi": round(fq["a"] + impute_hi + fq["b"] * cdd, 1),
            "delta_lo": round(fq["a"] + impute_lo + fq["b"] * cdd - before, 2),
            "delta_hi": round(fq["a"] + impute_hi + fq["b"] * cdd - before, 2),
        })

    # --- how many hot days a season would need for this to pay for itself
    #
    # delta(cdd) = A + B*cdd is the daily cost of the new setup versus the old.
    # A > 0 (more units idling) and B < 0 (each is less strained), so mild days
    # cost and hot days save. The question is whether a realistic number of hot
    # days can offset the rest of the season.
    A = fq["a"] + impute_lo - fp["a"]
    B = fq["b"] - fp["b"]
    payback = None
    hist_all = [(d, m, x) for d, (m, x) in weather_map(con).items() if d[5:7] in SUMMER]
    if hist_all:
        per_season_days = {}
        for d, m, _ in hist_all:
            per_season_days.setdefault(d[:4], []).append(max(0.0, m - BASE_F))
        full_seasons = {y: v for y, v in per_season_days.items() if len(v) >= 100}
        if full_seasons:
            hottest = max(m for _, m, _ in hist_all)
            hot_cdd = hottest - BASE_F
            delta_hot = A + B * hot_cdd          # kWh/day on the hottest day on record
            # cost carried by every day that does not save
            deficits = [statistics.mean(
                [max(0.0, A + B * c) for c in v]) * len(v) for v in full_seasons.values()]
            deficit = statistics.mean(deficits)
            saves = delta_hot < 0
            days_available = statistics.mean(
                [sum(1 for c in v if A + B * c < 0) for v in full_seasons.values()])
            payback = {
                "possible": bool(saves),
                "days_needed": round(deficit / -delta_hot, 1) if saves else None,
                "at_f": round(hottest, 1),
                "days_available": round(days_available, 1),
                "season_deficit_kwh": round(deficit, 1),
                "season_deficit_cost": round(deficit * rate, 2),
                "delta_at_hottest": round(delta_hot, 2),
                "seasons": sorted(full_seasons),
            }

    # --- how often the local climate actually reaches break-even
    climate = None
    hist = [(d, m, x) for d, (m, x) in weather_map(con).items() if d[5:7] in SUMMER]
    if hist and be_lo:
        per_season = {}
        for d, m, _ in hist:
            cdd = max(0.0, m - BASE_F)
            per_season.setdefault(d[:4], []).append(
                (fq["a"] + impute_lo + fq["b"] * cdd) - (fp["a"] + fp["b"] * cdd))
        # only seasons we hold in full can price a whole season, or the mean is
        # dragged down by however much of this year happens to be cached
        full = {y: v for y, v in per_season.items() if len(v) >= 100}
        be_f = BASE_F + be_lo                      # break-even as a temperature
        counted = [(d, m) for d, m, _ in hist if d[:4] in full]
        if full:
            climate = {
                "seasons": sorted(full),
                "days": len(counted),
                "high_gap": round(statistics.mean(
                    x - m for _, m, x in hist if x is not None), 1),
                "hottest_mean": round(max(m for _, m, _ in hist), 1),
                "days_above_be": round(
                    sum(1 for _, m in counted if m >= be_f) / len(full), 1),
                "season_cost": round(
                    statistics.mean(sum(v) * rate for v in full.values()), 0),
            }

    if payback:
        store.log_estimate(con, "season_cost", payback["season_deficit_cost"])
        store.log_estimate(con, "days_needed",
                           payback["days_needed"] if payback["possible"] else -1)
    history = store.estimate_history(con, "season_cost")

    return {
        "available": True,
        "history": [{"day": d, "season_cost": v} for d, v in history],
        "change": change, "resume": resume, "rate": rate,
        "new_units": sorted(new_units),
        "imputed": sorted(missing),
        "impute_band": [round(band[0], 1), round(band[1], 1)],
        "before": fp, "after": fq,
        "breakeven_lo": round(BASE_F + be_lo, 1) if be_lo else None,
        "breakeven_hi": round(BASE_F + be_hi, 1) if be_hi else None,
        "est_share_before": est_pre,
        "est_share_after": est_post,
        "extrapolated": bool(be_lo and be_lo > fp["cdd_hi"]),
        "observed_hi_f": round(BASE_F + fp["cdd_hi"], 1),
        "per_degree": round(-d_slope * rate, 3),
        "rows": rows,
        "climate": climate,
        "payback": payback,
        "weak": min(fp["r2"], fq["r2"]) < 0.5 or min(fp["n"], fq["n"]) < 10,
        "estimate_heavy": max(est_pre, est_post) >= 10,
    }
