#!/usr/bin/env python3
"""Weather-normalized before/after comparison around a change to the system.

    python3 compare.py 2026-08-08 --new "Kitchen,Living Room,Spare Room"

Comparing raw kWh before and after a change mostly measures the weather, not
the change. This regresses daily household kWh against cooling degree days
(daily mean temperature above 65F, from Open-Meteo for the ZIP in .env) for
each period, then predicts what each configuration would use on identically
warm days.

Read the R-squared and sample count before trusting the result: a handful of
post-change days spanning a narrow temperature band cannot support a
confident answer, however precise the numbers look.
"""
import argparse
import datetime as dt
import json
import time
import urllib.parse
import urllib.request

import store

BASE_F = 65.0
LAT, LON = 40.7128, -74.0060     # New Haven, CT (06512)


def daily_kwh(con, new_units):
    rows = con.execute("SELECT unit, hour, wh FROM hourly ORDER BY hour").fetchall()
    days = {}
    for r in rows:
        key = time.strftime("%Y-%m-%d", time.localtime(r["hour"]))
        e = days.setdefault(key, {"old": 0.0, "new": 0.0})
        e["new" if r["unit"] in new_units else "old"] += (r["wh"] or 0) / 1000
    return days


def weather(start, end):
    q = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON, "start_date": start, "end_date": end,
        "daily": "temperature_2m_mean", "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",
    })
    url = f"https://archive-api.open-meteo.com/v1/archive?{q}"
    with urllib.request.urlopen(url, timeout=30) as r:
        d = json.load(r)["daily"]
    return {day: t for day, t in zip(d["time"], d["temperature_2m_mean"]) if t is not None}


def fit(points):
    """Least squares kWh = a + b*CDD. Returns (a, b, r2, n)."""
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
    return a, b, (1 - ssr / sst) if sst else 0.0, n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("changepoint", help="YYYY-MM-DD the new units came online")
    p.add_argument("--new", default="", help="comma-separated names of the added units")
    p.add_argument("--settle", type=int, default=2,
                   help="days to discard after the change (install/setup noise)")
    p.add_argument("--rate", type=float, default=None, help="$/kWh override")
    args = p.parse_args()

    con = store.connect()
    rate = args.rate if args.rate is not None else float(
        store.get_meta(con, "rate_per_kwh", 0.105))
    new_units = {s.strip() for s in args.new.split(",") if s.strip()}

    days = daily_kwh(con, new_units)
    if not days:
        raise SystemExit("no history yet")
    lo, hi = min(days), max(days)
    wx = weather(lo, hi)

    change = args.changepoint
    resume = (dt.date.fromisoformat(change) + dt.timedelta(days=args.settle + 1)).isoformat()
    # the final day is usually partial; drop it
    partial = max(days)

    pre, post = [], []
    for day in sorted(days):
        if day not in wx or day == partial:
            continue
        cdd = max(0.0, wx[day] - BASE_F)
        total = days[day]["old"] + days[day]["new"]
        if day < change:
            pre.append((cdd, total))
        elif day >= resume:
            post.append((cdd, total))

    fp, fq = fit(pre), fit(post)
    print(f"history {lo} -> {hi}   change {change}   "
          f"discarded {change}..{resume} as install noise\n")
    for label, f, pts in (("BEFORE", fp, pre), ("AFTER ", fq, post)):
        if not f:
            print(f"{label}  too few days ({len(pts)})"); continue
        a, b, r2, n = f
        print(f"{label}  n={n:<3} kWh = {a:6.2f} + {b:5.2f} x CDD   R2={r2:.2f}   "
              f"CDD range {min(x for x,_ in pts):.1f}-{max(x for x,_ in pts):.1f}")

    if not (fp and fq):
        return
    ap, bp, r2p, np_ = fp
    aq, bq, r2q, nq = fq
    print(f"\nPredicted daily use at matched weather, at ${rate:.3f}/kWh:")
    print(f"{'mean F':>8}{'CDD':>6}{'before':>9}{'after':>9}{'change':>9}{'$/month':>10}")
    for cdd in (5, 7.5, 10, 12.5, 15):
        before, after = ap + bp * cdd, aq + bq * cdd
        delta = after - before
        print(f"{BASE_F+cdd:>8.1f}{cdd:>6}{before:>9.1f}{after:>9.1f}"
              f"{delta:>+9.1f}{delta*rate*30:>+10.2f}")

    if min(r2p, r2q) < 0.5 or min(np_, nq) < 10:
        print(f"\nWeak fit (R2 {r2p:.2f}/{r2q:.2f}, n {np_}/{nq}). Directional only -- "
              f"re-run once more days accumulate.")

    if new_units:
        old_pre = [days[d]["old"] for d in sorted(days) if d < change and d != partial]
        old_post = [days[d]["old"] for d in sorted(days) if d >= resume and d != partial]
        if old_pre and old_post:
            print(f"\nPre-existing units alone: {sum(old_pre)/len(old_pre):.1f} kWh/day before"
                  f" -> {sum(old_post)/len(old_post):.1f} kWh/day after")


if __name__ == "__main__":
    main()
