"""Where this month's electricity lands, and how much of that figure is a guess.

The dashboard can already say what the month has cost so far. The question a
person actually asks is the other one -- "so what is this month going to be?"
-- and the naive answer, month-to-date divided by days elapsed times days in
the month, is wrong in the one direction that matters: cooling load tracks the
weather, and the days still to come are almost never as hot as the ones just
gone.

So the remainder is projected the same way analysis.py normalises a before/after
comparison: regress daily household kWh on cooling degree days (daily mean above
BASE_F), then evaluate that line at the temperature the rest of the month is
actually expected to bring. There is no weather forecast in this system and
nothing here reaches the network for one, so "expected" means the climatological
normal for those calendar dates, taken from the summers already cached in
`weather`. Each cached year is also replayed in full as its own scenario, and
the spread between those years is the weather half of the reported band.

What this rests on, stated rather than buried:

  * The fit is per-household, not per-unit. Seven units share one set of
    weather, and a single unit's week of history is too thin to regress on its
    own; the projected remainder is split between units by each unit's share of
    energy over the fit window. A unit that starts behaving differently
    tomorrow will not be caught until it has moved the household total.
  * History before the system changed (config.CHANGE_DATE / config.MOVED_ON) is
    a different machine -- different units, in different rooms. The fit window
    starts after the change plus a settling allowance, which is what makes the
    fit short, and short is reported rather than smoothed over.
  * The normals for the rest of the month are usually cooler than anything in
    the fit window, so the daily figure is an extrapolation below the observed
    range. `extrapolated` says so, exactly as analysis.py does for its
    break-even point.
  * Month-to-date is read from the `hourly` rollup, part of which gapfill.py
    reconstructed rather than measured. That share is reported separately and
    never folded into the measured number.

When the weather is too thin to fit anything, or the fit is not credible, the
remainder falls back to a flat daily mean over the same window and says so.
When the month is too young to say anything at all, it refuses instead of
guessing.

Everything here reads; nothing here writes. `fetch` may top up the weather
cache, which is the one exception and can be turned off.
"""
import calendar
import math
import statistics
import time

from . import analysis, config, energy, store


#: A month needs at least this many complete days behind it before a projection
#: is worth printing. Two days of August cannot say anything about August, and
#: an honest refusal is more useful than a number with a huge band on it.
MIN_MONTH_DAYS = 3

#: Below this many days in the fit window, the degree-day regression is dropped
#: for a flat daily mean. Three points can be fitted arithmetically (analysis.fit
#: allows it) but a slope from three summer days is noise with a direction.
MIN_FIT_DAYS = 5

#: A fit explaining less of the variance than this is not telling us about the
#: weather, so the flat mean is the more honest of the two.
MIN_R2 = 0.4

#: Fit windows are capped here. Further back is more points but staler
#: behaviour -- setpoints, occupancy and which rooms are in use all drift.
FIT_WINDOW_DAYS = 30

#: Days to ignore after a change to the system before believing its energy,
#: matching the `settle` argument of analysis.comparison so the two agree about
#: which days describe the system as it stands.
SETTLE_DAYS = 2

#: A local day needs this many hours present in the rollup to count as observed.
#: The first and last day of local history are partial and would otherwise drag
#: a daily mean down as if the house had used less that day.
FULL_DAY_H = 20

#: Calendar-date normals need this many past years behind them, else the whole
#: month's normal is used instead of that one date's.
MIN_NORMAL_YEARS = 2

#: Floor on the daily scatter used to band a flat-mean projection, as a share
#: of the daily mean. Chosen from this system's own history: day-to-day
#: household energy swings by roughly a quarter of its mean across a summer
#: week, so a window of alike days reporting less than that is reporting the
#: window's luck rather than the method's accuracy.
FLAT_SPREAD = 0.25

#: A past summer is only replayed as a scenario if it covers this share of the
#: remaining dates -- a year holding three of fifteen days is not a scenario.
SCENARIO_COVER = 0.8

#: Flags for the UI, same thresholds analysis.py uses so the two pages do not
#: disagree about what counts as a thin result.
WEAK_N = 10
ESTIMATE_HEAVY = 10.0        # percent of month-to-date energy reconstructed


# ---------------------------------------------------------------- calendar

def month_bounds(now=None):
    """(start ts, end ts, "YYYY-MM", days in month) for the month `now` is in.

    The end is the first instant of the next month, built by walking to the
    last day rather than incrementing the month number, so December needs no
    special case and mktime resolves any DST shift itself.
    """
    lt = time.localtime(now if now is not None else time.time())
    n_days = calendar.monthrange(lt.tm_year, lt.tm_mon)[1]
    t0 = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    t1 = time.mktime((lt.tm_year, lt.tm_mon, n_days, 23, 59, 59, 0, 0, -1)) + 1
    return t0, t1, time.strftime("%Y-%m", lt), n_days


def _shift(day, n):
    """`day` plus n days as YYYY-MM-DD, anchored at noon so that a clock change
    cannot leave the arithmetic on the neighbouring date."""
    t = time.mktime(time.strptime(day, "%Y-%m-%d")) + n * 86400 + 43200
    return time.strftime("%Y-%m-%d", time.localtime(t))


# ---------------------------------------------------------------- history

def _observed_days(con):
    """({day: {unit: kWh}}, [days complete enough to average or fit on]).

    Completeness is counted in distinct hours present, not rows: units come and
    go, so a row count says as much about how many units were installed as about
    how much of the day was covered.
    """
    days, _est = analysis.daily_totals(con)
    hours = {}
    for r in con.execute("SELECT hour FROM hourly GROUP BY hour"):
        d = time.strftime("%Y-%m-%d", time.localtime(r["hour"]))
        hours[d] = hours.get(d, 0) + 1
    return days, sorted(d for d in days if hours.get(d, 0) >= FULL_DAY_H)


def _regime_start():
    """(day the system last changed, first day that describes it as it is now).

    A unit added, or moved to another room, makes every earlier day a
    measurement of a different house. Both dates are returned because they
    answer different questions: whether a change has happened at all, and
    which days are clean of it. (None, None) when nothing is declared, in
    which case all of history is fair game.
    """
    marks = [d for d in (config.CHANGE_DATE, config.MOVED_ON) if d]
    if not marks:
        return None, None
    return max(marks), _shift(max(marks), SETTLE_DAYS + 1)


def _profile(con, days):
    """Share of a day's energy falling in each local hour, over `days`.

    Used to price the part of today that has not happened yet. A flat
    seconds-remaining fraction would badly under-count an evening: cooling is
    not spread evenly over a day, and at 6pm most of it is still ahead.
    """
    acc = [0.0] * 24
    for r in con.execute("SELECT hour, wh FROM hourly"):
        lt = time.localtime(r["hour"])
        if time.strftime("%Y-%m-%d", lt) in days:
            acc[lt.tm_hour] += r["wh"] or 0.0
    total = sum(acc)
    return [a / total for a in acc] if total > 0 else None


def _day_left(profile, now):
    """Share of a typical day's energy still ahead of `now`."""
    lt = time.localtime(now)
    part = (lt.tm_min * 60 + lt.tm_sec) / 3600.0
    if not profile:
        return max(0.0, 1.0 - (lt.tm_hour + part) / 24.0)
    return (sum(profile[h] for h in range(lt.tm_hour + 1, 24))
            + profile[lt.tm_hour] * (1.0 - part))


# ---------------------------------------------------------------- weather

def normals(con, dates):
    """Expected cooling degree days for `dates`, and the years behind them.

    Returns ({date: cdd}, {year: {date: cdd}}). A calendar date's normal is the
    mean of that same date across *past* summers; with too few years for that,
    the mean of the whole month stands in, which is coarser but does not swing
    on one hot afternoon three years ago.

    The current year is excluded entirely. It holds the days being projected
    over -- for a projection re-run at an earlier date, some of those days have
    since happened, and letting them into the normals would be forecasting the
    weather from a record of the weather.
    """
    wx = analysis.weather_map(con)
    this_year = dates[0][:4] if dates else ""
    by_md, by_month = {}, {}
    years = {}
    for day, (mean_f, _max_f) in wx.items():
        if mean_f is None or day[:4] == this_year:
            continue
        cdd = max(0.0, mean_f - analysis.BASE_F)
        by_md.setdefault(day[5:], []).append(cdd)
        by_month.setdefault(day[5:7], []).append(cdd)
        years.setdefault(day[:4], {})[day[5:]] = cdd

    normal = {}
    for date in dates:
        md, mon = date[5:], date[5:7]
        seen = by_md.get(md, [])
        if len(seen) >= MIN_NORMAL_YEARS:
            normal[date] = statistics.fmean(seen)
        elif by_month.get(mon):
            normal[date] = statistics.fmean(by_month[mon])
    # a year is only a scenario if it covers most of the window
    want = max(1, int(len(dates) * SCENARIO_COVER))
    scenarios = {
        y: {d: v[d[5:]] for d in dates if d[5:] in v}
        for y, v in years.items()
        if sum(1 for d in dates if d[5:] in v) >= want
    }
    return normal, scenarios


# ---------------------------------------------------------------- projection

def project(con, now=None, rate=None, fetch=True):
    """Project this month's electricity, per unit and in total, with a band.

    `fetch` allows topping up the weather cache for days already elapsed; set
    it False to guarantee the call touches nothing. Returns
    {"available": False, "reason": ...} when the month is too young to say
    anything -- deliberately, rather than extrapolating one day into thirty.
    """
    now = float(now if now is not None else time.time())
    rate = rate if rate is not None else energy.rate(con)
    t0, t1, month, n_days = month_bounds(now)
    today = time.strftime("%Y-%m-%d", time.localtime(now))

    days, complete = _observed_days(con)
    in_month = [d for d in complete if d.startswith(month) and d < today]
    if len(in_month) < MIN_MONTH_DAYS:
        return {
            "available": False,
            "month": month,
            "days_observed": len(in_month),
            "reason": (f"Only {len(in_month)} complete day"
                       f"{'' if len(in_month) == 1 else 's'} of {month} so far; "
                       f"{MIN_MONTH_DAYS} are needed before a projection means "
                       f"anything."),
        }

    if fetch and complete:
        # elapsed days only -- the archive has nothing to say about the future
        analysis.ensure_weather(con, min(complete), today, timeout=8)
    wx = analysis.weather_map(con)

    # ---- what has actually happened, straight from the rollup
    mtd = store.rollup_range(con, t0, now)
    mtd_kwh = sum((r.get("wh") or 0) for r in mtd.values()) / 1000
    mtd_est = sum((r.get("est_wh") or 0) for r in mtd.values()) / 1000
    est_share = round(100 * mtd_est / mtd_kwh, 1) if mtd_kwh else 0.0

    # ---- the days that describe the system as it stands now
    #
    # Strictly before `today`: nothing may be fitted on days later than the one
    # being projected from, or a projection re-run over past dates would quietly
    # grade itself against the answer.
    changed_on, regime = _regime_start()
    if changed_on and changed_on > today:
        # the change is still ahead of the day being projected from, so as far
        # as this projection is concerned it has not happened
        changed_on = regime = None
    horizon = _shift(today, -FIT_WINDOW_DAYS)
    window = [d for d in complete
              if horizon <= d < today and (not regime or d >= regime)]
    fell_back_window = False
    if len(window) < MIN_MONTH_DAYS:
        # the change is too recent to have left a window; the month's own days
        # are the next best thing, and the caveat below says what that means
        window = in_month
        fell_back_window = True

    caveats = []
    if changed_on and fell_back_window:
        caveats.append(
            f"The system changed on {changed_on} and has not run long enough "
            f"since for a window of its own, so days from before the change are "
            f"being used to project days after it.")

    # ---- fit daily household kWh against degree days
    points = [(max(0.0, wx[d][0] - analysis.BASE_F), sum(days[d].values()))
              for d in window if d in wx]
    f = analysis.fit(points) if len(points) >= MIN_FIT_DAYS else None
    rmse = None
    if f:
        # analysis.fit reports r2 but not scatter, and the band needs scatter
        resid = [y - (f["a"] + f["b"] * x) for x, y in points]
        rmse = math.sqrt(sum(e * e for e in resid) / max(1, len(points) - 2))
    # a non-positive slope would mean cooling costs less the hotter it gets,
    # which is not a weak fit but a wrong one
    fitted = bool(f and f["r2"] >= MIN_R2 and f["b"] > 0)

    window_kwh = {}
    for d in window:
        for unit, kwh in days[d].items():
            window_kwh[unit] = window_kwh.get(unit, 0.0) + kwh
    window_total = sum(window_kwh.values())

    # ---- what is left of the month, and how warm it is expected to be
    dom = int(today[8:])
    dates = [f"{month}-{d:02d}" for d in range(dom, n_days + 1)]
    weights = {d: 1.0 for d in dates}
    weights[today] = _day_left(_profile(con, set(window)), now)
    left_days = sum(weights.values())

    normal, scenarios = normals(con, dates)
    # today's own weather is measured (if the archive has caught up), so prefer
    # it over the normal for the one day we are standing in
    def cdd_for(date, source):
        if date == today and today in wx:
            return max(0.0, wx[today][0] - analysis.BASE_F)
        if date in source:
            return source[date]
        return normal.get(date)

    have_normals = all(cdd_for(d, normal) is not None for d in dates)

    def remainder(source):
        return sum(weights[d] * max(0.0, f["a"] + f["b"] * cdd_for(d, source))
                   for d in dates)

    window_daily = [sum(days[d].values()) for d in window]
    daily_mean = statistics.fmean(window_daily)

    # Two different errors, and they do not combine the same way.
    #
    #   noise      -- day-to-day scatter around the line. Independent per day,
    #                 so it grows as the square root of the days left. That is
    #                 the optimistic reading (a fortnight-long heatwave is one
    #                 error, not fourteen) and the weather scenarios below are
    #                 what covers the correlated case.
    #   parameters -- the line itself is drawn from a handful of days. That
    #                 error is the same on every remaining day, so it adds up
    #                 rather than averaging out, and it grows with distance
    #                 from the temperatures the fit saw. This is the term that
    #                 makes an extrapolated projection say how thin it is,
    #                 instead of inheriting the tight scatter of a short window.
    noise_days = math.sqrt(sum(w * w for w in weights.values()))
    if fitted and have_normals:
        method = "degree-day"
        rem = remainder(normal)
        runs = [remainder(s) for s in scenarios.values()] or [rem]
        rem_lo, rem_hi = min(runs + [rem]), max(runs + [rem])
        xbar = statistics.fmean([x for x, _ in points])
        sxx = sum((x - xbar) ** 2 for x, _ in points) or 1e-9
        model_err = rmse * noise_days + sum(
            weights[d] * rmse * math.sqrt(
                1.0 / f["n"] + (cdd_for(d, normal) - xbar) ** 2 / sxx)
            for d in dates)
    else:
        method = "daily-mean"
        rem = rem_lo = rem_hi = daily_mean * left_days
        # A flat mean's real error is dominated by the weather it is ignoring,
        # which its own scatter cannot see -- a run of similar days looks
        # precise and is not. The floor keeps a handful of alike days from
        # reporting a confident band around a method that has no idea what is
        # coming.
        spread = max(statistics.pstdev(window_daily) if len(window_daily) > 1
                     else 0.0, daily_mean * FLAT_SPREAD)
        # same split: scatter around the mean, plus the error in the mean itself
        model_err = spread * noise_days + left_days * spread / math.sqrt(
            len(window_daily))
        tail = (" It will read high if the days ahead are cooler than the ones "
                "behind, which late in a summer they usually are.")
        if f is None:
            caveats.append(
                f"Only {len(points)} day{'' if len(points) == 1 else 's'} with "
                f"both energy and weather in the window -- too few to fit a "
                f"degree-day line, so the rest of the month is a flat daily "
                f"average of {daily_mean:.1f} kWh." + tail)
        elif not fitted:
            caveats.append(
                f"The degree-day fit explains too little of the variation "
                f"(r2 {f['r2']:.2f}) to project from, so the rest of the month "
                f"is a flat daily average of {daily_mean:.1f} kWh." + tail)
        else:
            caveats.append(
                "No cached weather for the dates still to come, so the rest of "
                "the month is a flat daily average rather than a weather fit."
                + tail)

    rem_lo = max(0.0, rem_lo - model_err)
    rem_hi = rem_hi + model_err

    cdds = [c for c in (cdd_for(d, normal) for d in dates) if c is not None]
    cdd_mean = statistics.fmean(cdds) if cdds else None
    extrapolated = bool(fitted and cdd_mean is not None
                        and (cdd_mean < f["cdd_lo"] or cdd_mean > f["cdd_hi"]))

    # ---- per unit: measured month-to-date, plus its share of the remainder
    floors = store.get_floors(con)
    units = []
    for device in config.DEVICES:
        name = device["name"]
        row = mtd.get(name, {})
        kwh = (row.get("wh") or 0) / 1000
        est = (row.get("est_wh") or 0) / 1000
        seen = sum(1 for d in in_month if name in days[d])
        share = (window_kwh.get(name, 0.0) / window_total) if window_total else 0.0
        display, prior = energy.room_label(name, t0, t1)
        units.append({
            "unit": name,
            "display": display,
            "prior_room": prior,
            "floor": floors.get(name),
            "kwh": round(kwh, 3),
            "cost": round(kwh * rate, 2),
            "est_kwh": round(est, 3),
            "est_share": round(100 * est / kwh, 1) if kwh > 0 else 0.0,
            "est_methods": sorted(m for m in (row.get("methods") or "").split(",") if m),
            "days_seen": seen,
            "partial_month": seen < len(in_month),
            "share": round(100 * share, 1),
            "projected_kwh": round(kwh + share * rem, 2),
            "projected_cost": round((kwh + share * rem) * rate, 2),
            "projected_lo": round((kwh + share * rem_lo) * rate, 2),
            "projected_hi": round((kwh + share * rem_hi) * rate, 2),
        })
    units.sort(key=lambda u: -u["projected_cost"])

    # ---- caveats worth printing next to the number
    if fitted and f["n"] < WEAK_N:
        caveats.append(
            f"The fit has only {f['n']} days behind it, from {window[0]} on.")
    if extrapolated:
        caveats.append(
            f"The rest of {month} is expected to average "
            f"{analysis.BASE_F + cdd_mean:.0f}F, outside the "
            f"{analysis.BASE_F + f['cdd_lo']:.0f}-{analysis.BASE_F + f['cdd_hi']:.0f}F "
            f"the fit was built on, so the daily figure is an extrapolation.")
    if est_share >= ESTIMATE_HEAVY:
        caveats.append(
            f"{est_share}% of the month-to-date energy was reconstructed by "
            f"gapfill, not measured.")
    partial = [u["display"] or u["unit"] for u in units if u["partial_month"]]
    if partial:
        caveats.append(
            f"{', '.join(partial)} {'has' if len(partial) == 1 else 'have'} no "
            f"history for part of {month}, so their month-to-date totals are not "
            f"a whole month's use. The projection adds only the days still to come.")
    if today in wx and weights[today] >= 0.05:
        caveats.append(
            "Today's outdoor mean is the archive's running figure for a day "
            "that is not over, so today's share of the projection will move.")

    total = mtd_kwh + rem
    return {
        "available": True,
        "month": month,
        "days_in_month": n_days,
        "now": int(now),
        "rate": rate,
        "method": method,
        "measured": {
            "kwh": round(mtd_kwh, 2),
            "cost": round(mtd_kwh * rate, 2),
            "days": len(in_month),
            "through": today,
            "est_kwh": round(mtd_est, 2),
            "est_share": est_share,
            "estimated_units": [u["unit"] for u in units if u["est_kwh"] > 0.01],
        },
        "remaining": {
            "days": round(left_days, 2),
            "today_left": round(weights[today], 3),
            "kwh": round(rem, 2),
            "kwh_lo": round(rem_lo, 2),
            "kwh_hi": round(rem_hi, 2),
            "cost": round(rem * rate, 2),
            "cost_lo": round(rem_lo * rate, 2),
            "cost_hi": round(rem_hi * rate, 2),
            "cdd_mean": round(cdd_mean, 2) if cdd_mean is not None else None,
            "mean_f": round(analysis.BASE_F + cdd_mean, 1) if cdd_mean is not None else None,
        },
        "month_total": {
            "kwh": round(total, 2),
            "kwh_lo": round(mtd_kwh + rem_lo, 2),
            "kwh_hi": round(mtd_kwh + rem_hi, 2),
            "cost": round(total * rate, 2),
            "cost_lo": round((mtd_kwh + rem_lo) * rate, 2),
            "cost_hi": round((mtd_kwh + rem_hi) * rate, 2),
        },
        "basis": {
            "days": len(window),
            "from": window[0] if window else None,
            "to": window[-1] if window else None,
            "regime_start": regime,
            "daily_mean_kwh": round(daily_mean, 2),
            "fitted": fitted,
            "r2": round(f["r2"], 3) if f else None,
            "kwh_per_cdd": round(f["b"], 2) if f else None,
            "base_kwh": round(f["a"], 2) if f else None,
            "rmse_kwh": round(rmse, 2) if rmse is not None else None,
            "model_err_kwh": round(model_err, 2),
            "cdd_lo": round(f["cdd_lo"], 1) if f else None,
            "cdd_hi": round(f["cdd_hi"], 1) if f else None,
            "extrapolated": extrapolated,
            "weak": bool(not fitted or f["n"] < WEAK_N),
            "scenario_years": sorted(scenarios),
        },
        "units": units,
        "caveats": caveats,
    }
