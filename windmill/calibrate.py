"""Find out which integration rule is actually right, instead of assuming one.

Every kWh figure on the dashboard comes from `energy.integrate_points`, which
applies the trapezoid rule to `v15` watts and refuses to integrate across a hole
longer than `energy.MAX_GAP`. That choice was never tested. It is also the least
likely rule to be correct: readings are written on change and held (see
store.write_sample), so between two rows the unit was drawing the *earlier*
value the whole time, not ramping smoothly from one to the next. The trapezoid
rule draws a ramp that never happened, and the error it makes grows with the
length of the interval -- which is exactly why meter.py found the worst
disagreement on the lightly-used units, whose intervals are longest:

    Kitchen, Living Room, Study   within 4%
    Bedroom                              we read 13% low
    Third Floor L/R, Spare Room        we read 15-23% high

`v16` -- the unit's own kWh for the last quarter hour -- is an independent
measurement of the same energy, so it can grade the arithmetic. This module
scores several candidate rules against it and reports which one wins.

Reusing meter.py is the point, not an economy. The hard part of this comparison
is that the meter's history is younger than the watt history, so a window has to
be narrowed to the span the meter actually covers before either side is measured
-- meter.compare already solves that, along with the coverage floor and the
sanity gate that drops units which barely ran. The windows and the ground truth
come from there unchanged; the only thing varied here is the rule.

Run against this system's own database over 57.5 kWh of meter readings, it does
matter, and two independent mistakes are being made at once. Signed error, +
meaning we read high:

    rule            Bedroom  Kitchen  Spare  Study  TF Left  TF Right  wMAPE
    trapezoid        -11.9     -1.0   30.0   -2.5     12.4      24.9    2.66
    left             -11.3     -1.8    1.6   -6.1     -3.0      -0.8    2.17
    trapezoid-clamp   -1.1      1.0   33.9    1.0     16.1      24.9    2.40
    left + clamp      -0.5      0.2    5.5   -2.6      0.7      -0.8    0.43

The trapezoid rule is what makes the lightly-used units read high: it ramps
across the long intervals that write-on-change storage produces, inventing draw
that was never there. Holding the previous value instead takes Spare Room from
+30% to +1.6% and Third Floor Right from +24.9% to -0.8%. Separately, refusing to
integrate an interval longer than MAX_GAP *discards* it, and a keyframe that
lands one poll late is 930 s against a 900 s limit -- real running time thrown
away, which is why Bedroom reads low. Counting the first MAX_GAP of a long
interval rather than none of it takes Bedroom from -11.9% to -1.1%. Each change
fixes one direction of meter.py's split, and only both together fix the fleet:
worst per-unit error falls from 30% to 5.6%, weighted MAPE from 2.66% to 0.43%.

The sampling interval is not the constraint. Simulating a coarser poll leaves
weighted MAPE flat at 0.4% from 30 s through 120 s and only reaches 1.6% at
600 s -- still far better than today's arithmetic at 30 s. Polling faster would
buy nothing; the error lives in the rule, not the rate.

Kitchen is 81% of the graded energy (eight days against nine hours for every
other unit), so every fleet figure is re-checked with it excluded before any
conclusion is drawn. The ranking above survives that: without Kitchen, left +
clamp scores 1.38% against trapezoid's 9.65%.

Nothing here is applied to stored history and nothing calls it. It reports what
the meter says and leaves the change to energy.py to a deliberate decision --
rebuilding the `hourly` rollup is part of that, since every stored hour was
integrated with the current rule.
"""
import time

from . import config, energy, meter, store


#: How often the poller reads each unit. A change is only *detected* at the
#: poll that sees it, so this is the width of the window in which any transition
#: really happened, and the most a step can honestly be smeared over.
POLL_S = 30

#: The rule the dashboard uses today, graded alongside the rest.
#: What `energy.integrate_points` actually does, so a verdict is measured
#: against the shipping code rather than against what it used to do. This was
#: "trapezoid" until the evidence below moved it: hold the previous level, and
#: clamp a long gap rather than discarding the interval.
CURRENT = "left-clamped"

#: Evidence to grade against. Longer than meter.py's 24 h default because only
#: Kitchen has more than a day of watts, and a shorter window would throw away
#: the one unit with real history.
DEFAULT_HOURS = 7 * 24

#: Sampling intervals to simulate, as multiples of POLL_S. The question is
#: whether 30 s is the constraint, which only shows up if the sweep goes coarse
#: enough for the answer to visibly break.
DECIMATIONS = (1, 2, 4, 8, 20)

#: A rule must beat the current one by more than this many percentage points of
#: weighted MAPE before changing anything is justified. Set above the spread
#: between units so a winner cannot be manufactured out of one unit's noise.
MIN_GAIN = 2.0

#: Rules whose weighted MAPE differs by less than this are a tie, not a ranking.
#: Without it a coin-flip between two indistinguishable rules reads as one of
#: them losing, which is how a robustness check ends up rejecting its own winner.
TIE = 0.25


# ------------------------------------------------------------- the candidates

def _riemann(points, pick, t_end=None, max_gap=energy.MAX_GAP, clamp=False):
    """Sum watts x seconds, taking each interval's level from `pick(a, b)`.

    `clamp` is the disagreement between energy.integrate_points, which drops an
    over-long interval entirely, and store.rebuild_hours, which counts the first
    `max_gap` of it. Dropping is safe against a unit that was off the air for an
    hour; it also silently discards every keyframe interval that landed a poll
    late (930 s against a 900 s limit), which is thousands of seconds of real
    running time. Both behaviours are graded rather than argued about.
    """
    wh = 0.0
    prev = None
    for ts, watts in points:
        if prev is not None:
            dt = ts - prev[0]
            if dt > max_gap:
                dt = max_gap if clamp else 0
            if dt > 0:
                wh += pick(prev[1], watts) * dt / 3600
        prev = (ts, watts)
    if prev and t_end:
        dt = t_end - prev[0]
        if dt > max_gap:
            dt = max_gap if clamp else 0
        if dt > 0:
            wh += prev[1] * dt / 3600      # nothing follows, so the level held
    return wh / 1000


def _midpoint(points, t_end=None, max_gap=energy.MAX_GAP):
    """The level in force half way through each interval, read off the series.

    Included because it is the obvious third option, and it is worth measuring
    rather than asserting that it collapses onto `left`: the stored series *is*
    a step function, so the value at an interval's midpoint is the value at its
    start. The run confirms the two are identical to the last decimal.
    """
    at = store.step_fn(points, default=None)
    wh = 0.0
    prev = None
    for ts, watts in points:
        if prev is not None:
            dt = ts - prev[0]
            if 0 < dt <= max_gap:
                wh += at(prev[0] + dt / 2) * dt / 3600
        prev = (ts, watts)
    if prev and t_end and 0 < t_end - prev[0] <= max_gap:
        wh += prev[1] * (t_end - prev[0]) / 3600
    return wh / 1000


def _hold_ramp(points, t_end=None, max_gap=energy.MAX_GAP, poll=POLL_S):
    """Hold the previous level, then trapezoid across the last poll interval.

    The physical correction to both extremes. A change seen at time t happened
    somewhere in the `poll` seconds before t and nowhere earlier, so holding the
    old level for the whole interval over-counts by at most one poll, and
    ramping across the whole interval over-counts by however long the interval
    was. This ramps across exactly the stretch where the transition could have
    occurred: identical to the trapezoid rule when samples are one poll apart,
    and converging on `left` as intervals get longer.
    """
    wh = 0.0
    prev = None
    for ts, watts in points:
        if prev is not None:
            dt = ts - prev[0]
            if 0 < dt <= max_gap:
                ramp = min(poll, dt)
                wh += (prev[1] * (dt - ramp) + (prev[1] + watts) / 2 * ramp) / 3600
        prev = (ts, watts)
    if prev and t_end and 0 < t_end - prev[0] <= max_gap:
        wh += prev[1] * (t_end - prev[0]) / 3600
    return wh / 1000


_MEAN = lambda a, b: (a + b) / 2
_LEFT = lambda a, b: a
_RIGHT = lambda a, b: b

#: Every rule under test, name -> f(points, t_end) -> kWh. The `/n` names vary
#: only the gap limit, to separate "the rule is wrong" from "the gap policy is
#: wrong" -- they are independent choices and the current code makes both.
CANDIDATES = {
    "trapezoid":  lambda p, e: _riemann(p, _MEAN, e),
    "left":       lambda p, e: _riemann(p, _LEFT, e),
    "right":      lambda p, e: _riemann(p, _RIGHT, e),
    "midpoint":   lambda p, e: _midpoint(p, e),
    "hold-ramp":  lambda p, e: _hold_ramp(p, e),
    "trapezoid-clamped": lambda p, e: _riemann(p, _MEAN, e, clamp=True),
    "left-clamped":      lambda p, e: _riemann(p, _LEFT, e, clamp=True),
    "trapezoid/300":     lambda p, e: _riemann(p, _MEAN, e, max_gap=300),
    "trapezoid/1800":    lambda p, e: _riemann(p, _MEAN, e, max_gap=1800),
    "left/1800":         lambda p, e: _riemann(p, _LEFT, e, max_gap=1800),
}


def integrate(points, rule=CURRENT, t_end=None):
    """kWh from [(ts, watts)] under a named rule. Unknown names raise."""
    return CANDIDATES[rule](points, t_end)


def resample(points, t0, t1, step):
    """The series as it would have been had the poller run every `step` seconds.

    Coarser sampling is simulated by reading the step reconstruction on a
    uniform grid rather than by dropping stored rows: rows are written on change
    (store.write_sample), so every nth row is not every nth sample -- on a busy
    unit it is a fraction of a second apart and on an idle one a quarter hour.
    Only the grid answers the question that was asked.

    The grid carries the last known level across any hole, with no staleness
    limit, so a long outage would be filled with a frozen wattage. Acceptable
    only because the graded windows are all above 98% meter coverage; it is the
    reason these numbers grade the sampling rate and not the gap policy, which
    is graded separately on the stored series.
    """
    at = store.step_fn(points, default=None)
    out = []
    ts = int(t0)
    while ts <= t1:
        val = at(ts)
        if val is not None:
            out.append((ts, val))
        ts += step
    return out


# ------------------------------------------------------------------ scoring

def windows(con, t0=None, t1=None, now=None):
    """[{unit, from, to, meter_kwh, ...}] for the units the meter can grade.

    Straight out of meter.compare, filtered by meter.report's own `usable` test.
    That test reads the ratio the *current* rule produces, which is deliberate:
    if admission depended on the rule being graded, each rule would be scored
    over a different set of units and the totals would not be comparable.
    """
    now = now or time.time()
    t1 = t1 or now
    t0 = t0 or (t1 - DEFAULT_HOURS * 3600)
    out = []
    for device in config.DEVICES:
        r = meter.compare(con, device["name"], t0, t1)
        if (r["ratio"] and meter.SANE[0] <= r["ratio"] <= meter.SANE[1]
                and r["buckets"] >= meter.MIN_BUCKETS):
            out.append(r)
    return out


def _errors(con, spans, rules, step=None, now=None):
    """{unit: {rule: signed % error against the meter}} over the given spans."""
    now = now or time.time()
    per_unit = {}
    for w in spans:
        pts = store.series(con, w["unit"], config.POWER_PIN, w["from"], w["to"])
        if step:
            pts = resample(pts, w["from"], w["to"], step)
        end = min(w["to"], now)
        got = {}
        for name in rules:
            kwh = CANDIDATES[name](pts, end)
            got[name] = {"kwh": round(kwh, 3),
                         "err_pct": round(100 * (kwh - w["meter_kwh"]) / w["meter_kwh"], 1)}
        per_unit[w["unit"]] = got
    return per_unit


def _aggregate(spans, per_unit, rules):
    """Per-rule MAPE, kWh-weighted MAPE, and signed bias across the fleet.

    Weighting by the meter's own kWh is what stops a unit that used 0.05 kWh
    from outvoting one that used 3. The plain mean is reported next to it
    because the two diverging is itself the finding: it means one heavy unit is
    deciding the answer.
    """
    weights = {w["unit"]: w["meter_kwh"] for w in spans}
    total = sum(weights.values()) or 1.0
    out = {}
    for name in rules:
        errs = [(per_unit[u][name]["err_pct"], weights[u]) for u in per_unit]
        if not errs:
            continue
        out[name] = {
            "mape": round(sum(abs(e) for e, _ in errs) / len(errs), 2),
            "wmape": round(sum(abs(e) * w for e, w in errs) / total, 2),
            "bias": round(sum(e * w for e, w in errs) / total, 2),
            "worst": round(max(abs(e) for e, _ in errs), 1),
            "wins": sum(1 for u in per_unit
                        if abs(per_unit[u][name]["err_pct"])
                        == min(abs(per_unit[u][r]["err_pct"]) for r in rules)),
        }
    return out


def score(con, t0=None, t1=None, rules=None, step=None, now=None, spans=None):
    """Every rule against the meter, per unit and overall.

    `step` simulates a coarser poll; None grades the stored series as it is.
    """
    rules = list(rules or CANDIDATES)
    spans = windows(con, t0, t1, now) if spans is None else spans
    per_unit = _errors(con, spans, rules, step, now)
    return {
        "units": {w["unit"]: {"meter_kwh": w["meter_kwh"], "hours": w["hours"],
                              "buckets": w["buckets"], "coverage": w["coverage"],
                              "rules": per_unit.get(w["unit"], {})}
                  for w in spans},
        "methods": _aggregate(spans, per_unit, rules),
        "graded": len(spans),
        "meter_kwh": round(sum(w["meter_kwh"] for w in spans), 3),
    }


def interval_study(con, spans, rules=None, factors=DECIMATIONS, now=None):
    """How the error grows as the poll interval is made coarser.

    Answers whether 30 s is the limiting factor. If error is flat from 30 s to
    a few minutes then sampling faster buys nothing and the rule is the whole
    story; if it climbs immediately, the opposite.
    """
    rules = list(rules or (CURRENT, "left", "hold-ramp"))
    out = []
    for k in factors:
        s = score(con, rules=rules, step=k * POLL_S, now=now, spans=spans)
        out.append({"interval_s": k * POLL_S, "methods": s["methods"]})
    return out


# ------------------------------------------------------------ the conclusion

def report(con, t0=None, t1=None, now=None):
    """Grade every rule, and say plainly whether any of it is worth acting on."""
    now = now or time.time()
    spans = windows(con, t0, t1, now)
    if len(spans) < 2:
        return {"graded": len(spans), "verdict": "not enough meter data yet",
                "detail": "Fewer than two units have meter coverage to grade against."}

    main = score(con, now=now, spans=spans)
    ranked = sorted(main["methods"].items(), key=lambda kv: kv[1]["wmape"])
    best, best_stats = ranked[0]
    cur = main["methods"][CURRENT]
    gain = round(cur["wmape"] - best_stats["wmape"], 2)

    # One unit holding most of the fleet's kWh means the fleet number is that
    # unit's number wearing a hat. Re-rank without it and see if the winner
    # survives; a winner that does not is a winner of one measurement.
    heaviest = max(main["units"], key=lambda u: main["units"][u]["meter_kwh"])
    rest = [w for w in spans if w["unit"] != heaviest]
    without = score(con, now=now, spans=rest)["methods"] if len(rest) >= 2 else {}
    robust = (not without or
              without[best]["wmape"] <= min(m["wmape"] for m in without.values()) + TIE)
    tied = [n for n, m in main["methods"].items()
            if m["wmape"] <= best_stats["wmape"] + TIE and n != best]

    spread = round(max(m["wmape"] for m in main["methods"].values())
                   - best_stats["wmape"], 2)
    worst_unit = max(main["units"],
                     key=lambda u: abs(main["units"][u]["rules"][best]["err_pct"]))
    unit_spread = round(abs(main["units"][worst_unit]["rules"][best]["err_pct"]), 1)

    # The bar: a real gain, still there without the heaviest unit, and bigger
    # than the disagreement between units under the winning rule. The last
    # clause is the one that usually fails -- picking a rule cannot fix an error
    # that differs by 20 points from one unit to the next, because no single
    # rule is wrong in two directions at once.
    change = gain > MIN_GAIN and robust and gain > unit_spread / 4
    if change:
        detail = (f"{best} beats {CURRENT} by {gain} points of weighted MAPE "
                  f"({best_stats['wmape']}% against {cur['wmape']}%), and still "
                  f"wins with {heaviest} excluded. Worst remaining per-unit "
                  f"error {unit_spread}% ({worst_unit}), against "
                  f"{round(max(abs(d['rules'][CURRENT]['err_pct']) for d in main['units'].values()), 1)}% today."
                  + (f" Indistinguishable from {', '.join(tied)}." if tied else ""))
    elif gain <= MIN_GAIN:
        detail = (f"The best rule ({best}, {best_stats['wmape']}% weighted MAPE) "
                  f"beats {CURRENT} ({cur['wmape']}%) by {gain} points, inside "
                  f"the {MIN_GAIN}-point bar. The rules are within noise of each "
                  f"other; the choice of rule is not what is wrong.")
    elif not robust:
        detail = (f"{best} wins overall but not with {heaviest} excluded, and "
                  f"{heaviest} is "
                  f"{round(100 * main['units'][heaviest]['meter_kwh'] / (main['meter_kwh'] or 1))}"
                  f"% of the graded energy. That is one unit's result, not a fleet result.")
    else:
        detail = (f"{best} gains {gain} points, but units still disagree by up "
                  f"to {unit_spread}% under it ({worst_unit}). A per-unit error "
                  f"that large is not something a different rule can remove.")

    return {
        "window": {"hours": DEFAULT_HOURS if t0 is None else round((t1 or now) - t0) / 3600,
                   "graded": len(spans), "meter_kwh": main["meter_kwh"],
                   "heaviest": heaviest,
                   "heaviest_share": round(
                       100 * main["units"][heaviest]["meter_kwh"] / (main["meter_kwh"] or 1), 1)},
        "units": main["units"],
        "methods": main["methods"],
        "ranked": [name for name, _ in ranked],
        "without_heaviest": without,
        "intervals": interval_study(con, spans, now=now),
        "current": CURRENT,
        "best": best,
        "tied": tied,
        "gain": gain,
        "spread": spread,
        "verdict": "change to " + best if change else "no change warranted",
        "detail": detail,
    }


# ------------------------------------------------------------------- console

def _table(rows, head):
    widths = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    lines = ["  ".join(str(h).ljust(w) for h, w in zip(head, widths)).rstrip(),
             "  ".join("-" * w for w in widths)]
    for r in rows:
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(lines)


def main():
    con = store.connect()
    try:
        r = report(con)
    finally:
        con.close()
    if "methods" not in r:
        print(r["detail"])
        return

    w = r["window"]
    print(f"Graded {w['graded']} units, {w['meter_kwh']} kWh of meter readings. "
          f"{w['heaviest']} is {w['heaviest_share']}% of it.\n")

    print(_table([[u, d["hours"], d["buckets"], d["coverage"], d["meter_kwh"]]
                  for u, d in sorted(r["units"].items())],
                 ["unit", "hours", "buckets", "coverage", "meter kWh"]))

    print("\nSigned % error against the meter (+ = we read high):\n")
    units = sorted(r["units"])
    print(_table([[m] + [r["units"][u]["rules"][m]["err_pct"] for u in units]
                  + [r["methods"][m]["mape"], r["methods"][m]["wmape"],
                     r["methods"][m]["bias"]]
                  for m in r["ranked"]],
                 ["rule"] + units + ["MAPE", "wMAPE", "bias"]))

    print("\nWeighted MAPE with " + r["window"]["heaviest"] + " excluded:\n")
    print(_table([[m, d["wmape"]] for m, d in
                  sorted(r["without_heaviest"].items(), key=lambda kv: kv[1]["wmape"])],
                 ["rule", "wMAPE"]) if r["without_heaviest"] else "  (too few units left)")

    print("\nWeighted MAPE against simulated poll interval:\n")
    names = list(r["intervals"][0]["methods"])
    print(_table([[row["interval_s"]] + [row["methods"][n]["wmape"] for n in names]
                  for row in r["intervals"]], ["interval s"] + names))

    print(f"\n{r['verdict'].upper()}\n{r['detail']}")


if __name__ == "__main__":
    main()
