"""When each unit's air filter needs changing.

The unit answers this itself. `v11` is Windmill's **Filter Change Indicator**:
0 while the filter is fine, 1 once it wants changing. That was confirmed by
pulling `dataStreamId=12` from the export API -- whose CSV header names it
"Filter Change Indicator" -- and matching its values against the pin. So there
is no threshold to invent here: the unit is the authority on whether its own
filter is dirty, and this module's job is to report that, say how long the
filter has been in, and notice when one gets changed.

`v7` is Filter Runtime, cumulative hours since the filter was last reset. It is
what makes "due" actionable -- 851 hours is a different conversation from 190 --
and the runtime at which the indicator flips is *learned* from watching it flip
rather than guessed, so an early warning only ever rests on something observed.

A caution about v7: it has been seen to step backwards by tens of hours without
the indicator clearing (670 -> 621, 952 -> 849). That is not a filter change, so
a reset is taken from the indicator going 1 -> 0, not from the counter dropping.
"""
import time

from . import config, store


INDICATOR = "v11"       # 0 = fine, 1 = change the filter
RUNTIME = "v7"          # cumulative hours since the filter was last reset

#: Warn this fraction of the way to the runtime at which the indicator has
#: actually been seen to fire. Only used once such a flip has been observed.
WARN_AT = 0.85

#: History window for finding transitions. Filters last months, so this looks
#: back much further than the other checks.
LOOKBACK_S = 400 * 86400


def _transitions(con, unit, now):
    """[(ts, from, to)] for the indicator, oldest first."""
    rows = [(r["ts"], r["val"]) for r in con.execute(
        "SELECT ts, val FROM readings WHERE unit=? AND pin=? AND ts>=? ORDER BY ts",
        (unit, INDICATOR, now - LOOKBACK_S))]
    out, prev = [], None
    for ts, v in rows:
        if prev is not None and v != prev:
            out.append((ts, prev, v))
        prev = v
    return out


def learned_threshold(con, now):
    """Runtime hours at which the indicator has been seen to turn on.

    None until a 0 -> 1 flip has actually been observed. Everything that
    depends on this stays silent rather than falling back to a made-up number.
    """
    seen = []
    for device in config.DEVICES:
        unit = device["name"]
        for ts, was, is_ in _transitions(con, unit, now):
            if was == 0 and is_ == 1:
                hrs = store.step_fn(store.series(con, unit, RUNTIME,
                                                 ts - 86400, ts), default=None)(ts)
                if hrs is not None:
                    seen.append(hrs)
    return min(seen) if seen else None


def runtime_anomalies(con, unit, now=None, lookback=LOOKBACK_S):
    """Backwards steps in the runtime counter: [(ts, from, to)].

    `v7` is meant to be cumulative, but it has been seen to lose tens of hours
    at once (670 -> 621, 952 -> 849). Software power cycles were the obvious
    suspect and are not the cause -- 16 resyncs left it stable or rising, and
    neither drop had a resync within fifteen minutes of it. The cause is
    unknown, so the honest thing is to count them and mark the hours figure as
    a lower bound rather than quietly present a number that has lost time.
    """
    now = int(now or time.time())
    rows = [(r["ts"], r["val"]) for r in con.execute(
        "SELECT ts, val FROM readings WHERE unit=? AND pin=? AND ts>=? ORDER BY ts",
        (unit, RUNTIME, now - lookback))]
    return [(b[0], a[1], b[1]) for a, b in zip(rows, rows[1:]) if b[1] < a[1] - 0.5]


def report(con, now=None):
    """{unit: filter state}. `verdict` is due | soon | ok | unknown."""
    now = int(now or time.time())
    threshold = learned_threshold(con, now)
    # Until a flip has been watched, the most that can be said is that the
    # trip point is above the highest runtime still reporting a clean filter
    # and no higher than the lowest one already asking to be changed.
    clean_max = due_min = None

    rows = {}
    for device in config.DEVICES:
        unit = device["name"]
        due = store.latest(con, unit, INDICATOR)
        hours = store.latest(con, unit, RUNTIME)
        changes = [t for t, was, is_ in _transitions(con, unit, now)
                   if was == 1 and is_ == 0]
        drops = runtime_anomalies(con, unit, now)
        rows[unit] = {
            "unit": unit,
            "due": None if due is None else bool(due),
            "hours": None if hours is None else round(hours),
            "changed_at": changes[-1] if changes else None,
            "lost_hours": round(sum(a - b for _, a, b in drops)) or None,
            "drops": len(drops),
        }
        if due is not None and hours is not None:
            if due:
                due_min = hours if due_min is None else min(due_min, hours)
            else:
                clean_max = hours if clean_max is None else max(clean_max, hours)

    warn_at = (threshold * WARN_AT) if threshold else None
    for unit, r in rows.items():
        h, due = r["hours"], r["due"]
        r["threshold"] = threshold
        r["bracket"] = [clean_max, due_min] if threshold is None else None
        if due is None:
            r["verdict"] = "unknown"
            r["detail"] = "This unit has not reported its filter status."
        elif due:
            r["verdict"] = "due"
            r["detail"] = (f"The unit is asking for a filter change"
                           + (f", after {h} hours of running." if h is not None
                              else "."))
        elif warn_at and h is not None and h >= warn_at:
            r["verdict"] = "soon"
            r["detail"] = (f"{h} hours in. Filters here have asked to be changed "
                           f"at about {round(threshold)} hours.")
        else:
            r["verdict"] = "ok"
            r["detail"] = (f"{h} hours in." if h is not None else "Clean.")
        if r["lost_hours"]:
            r["detail"] += (f" Counter has lost {r['lost_hours']}h across "
                            f"{r['drops']} backwards step"
                            f"{'' if r['drops'] == 1 else 's'}, so the hours "
                            f"figure is a lower bound.")
    return rows


def due_now(con, now=None):
    """Just the units asking for a filter change."""
    return {u: r for u, r in report(con, now).items() if r["verdict"] == "due"}
