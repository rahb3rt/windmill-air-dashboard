"""Check our energy figures against the unit's own.

Everything on the dashboard integrates `v15` watts sampled every 30 seconds.
That is the only way to get energy per unit over an arbitrary range, but it is
an approximation of a compressor that switches abruptly: between two samples the
earlier value is held, so a spike that starts just after a sample is missed and
one that ends just after a sample is over-counted.

`v16` is the unit's own energy figure -- kWh for the last 15 minutes, replaced
each quarter hour. It is not a substitute (no history before this tool ran, no
per-range totals) but it is an independent measurement of the same thing, and
comparing them says how far off the integration is.

Measured across seven units, and stable between a six-hour and a
nine-hour window:

    Kitchen, Living Room, Study   within 4%
    Bedroom                              we read 13% low
    Third Floor L/R, Spare Room        we read 15-23% high

Negative means we report *more* energy than the unit did. The error is worst on
lightly-used units, which is the expected shape: fewer, shorter compressor
bursts mean more of the total rides on what happened between samples.

Nothing here is applied. A single fleet-wide factor comes out near 1 (0.94-0.97)
because the per-unit errors point in both directions and cancel -- applying it
would nudge the totals a few percent while leaving every individual figure
exactly as wrong as it was. The per-unit comparison is the useful output, and
the raw readings stay the measurement.
"""
import time

from . import blynk, config, energy, store


METER = "v16"           # unit's own kWh for the last ~15 min
BUCKET_S = 900          # how often it is replaced

#: Don't offer a correction from less evidence than this.
MIN_HOURS = 6
MIN_BUCKETS = 12

#: Ignore ratios outside this range when averaging: a unit that barely ran
#: produces huge ratios from rounding, not from integration error.
SANE = (0.25, 4.0)


def meter_buckets(con, unit, t0, t1):
    """[(ts, kWh)] of the unit's own energy readings, one per quarter hour.

    The pin holds a value until it is replaced, so the stored rows are already
    one per bucket -- but a keyframe rewrite would double-count, so identical
    consecutive values closer together than a bucket are collapsed.
    """
    rows = store.series(con, unit, METER, t0, t1)
    out, last_ts = [], None
    for ts, val in rows:
        if last_ts is not None and ts - last_ts < BUCKET_S * 0.6:
            continue
        out.append((int(ts), val))
        last_ts = ts
    return out


#: A comparison is only meaningful if the meter covers the window. Below this
#: it is refused rather than reported with a caveat.
MIN_COVERAGE = 0.9


def compare(con, unit, t0, t1):
    """Our integrated kWh against the unit's own, over the *same* span.

    The meter's history is younger than the watt history for most units -- its
    rows here start hours after the readings do. Comparing our figure for the
    whole window against a meter total covering part of it makes the
    integration look wildly high, which is a fact about the window and not
    about the arithmetic. So the window is narrowed to what the meter actually
    covers, and both sides are measured across that.
    """
    buckets = meter_buckets(con, unit, t0, t1)
    if len(buckets) < 2:
        return {"unit": unit, "ratio": None, "coverage": 0.0, "buckets": len(buckets),
                "ours_kwh": None, "meter_kwh": None, "delta_pct": None,
                "hours": 0.0, "from": None, "to": None}

    # The meter reports energy for the quarter hour *ending* at its timestamp,
    # so the span it describes starts one bucket before the first reading.
    m0, m1 = buckets[0][0] - BUCKET_S, buckets[-1][0]
    span = max(1, m1 - m0)
    gaps = [b[0] - a[0] for a, b in zip(buckets, buckets[1:])]
    covered = BUCKET_S + sum(min(g, BUCKET_S) for g in gaps)
    coverage = min(1.0, covered / span)

    pts = store.series(con, unit, config.POWER_PIN, m0, m1)
    ours, _, _ = energy.integrate_points(pts, t_end=min(m1, time.time()))
    theirs = sum(v for _, v in buckets)
    ratio = (theirs / ours) if ours > 0.001 and coverage >= MIN_COVERAGE else None
    return {
        "unit": unit,
        "ours_kwh": round(ours, 3),
        "meter_kwh": round(theirs, 3),
        "buckets": len(buckets),
        "coverage": round(coverage, 3),
        "hours": round(span / 3600, 1),
        "from": int(m0), "to": int(m1),
        "ratio": round(ratio, 3) if ratio else None,
        "delta_pct": round((ratio - 1) * 100, 1) if ratio else None,
    }


def report(con, t0=None, t1=None, now=None):
    """Per-unit comparison plus a fleet-wide correction factor if earned."""
    now = now or time.time()
    t1 = t1 or now
    t0 = t0 or (t1 - 24 * 3600)
    rows = {d["name"]: compare(con, d["name"], t0, t1) for d in config.DEVICES}

    usable = [r for r in rows.values()
              if r["ratio"] and SANE[0] <= r["ratio"] <= SANE[1]
              and r["buckets"] >= MIN_BUCKETS]
    factor = None
    if usable:
        total_meter = sum(r["meter_kwh"] for r in usable)
        if total_meter > 0.05:
            factor = round(total_meter / sum(r["ours_kwh"] for r in usable), 4)

    for r in rows.values():
        d = r["delta_pct"]
        r["verdict"] = ("not enough meter data" if not r["ratio"]
                        else "close" if abs(d) <= 5
                        else "we read high" if d < 0 else "we read low")
    off = [r for r in usable if abs(r["delta_pct"]) > 10]
    return {
        "units": rows,
        "factor": factor,
        "window": {"from": int(t0), "to": int(t1),
                   "hours": round((t1 - t0) / 3600, 1)},
        "usable": len(usable),
        "off": sorted(r["unit"] for r in off),
        # Deliberately advisory. A single fleet factor is near 1 because the
        # per-unit errors point in both directions and cancel, so applying one
        # would move the totals by a couple of percent while leaving every
        # individual figure exactly as wrong as it was. The per-unit numbers
        # are the useful output; nothing here is applied to stored history.
        "detail": ((f"Fleet-wide the meter totals {factor:.3f}× our integration"
                    + (f", but {len(off)} unit(s) differ by more than 10%: "
                       + ", ".join(sorted(r["unit"] for r in off)) + "."
                       if off else " and no unit differs by more than 10%."))
                   if factor else
                   "Not enough overlapping meter data yet to compare."),
    }


def factor(con, t0=None, t1=None):
    """Just the correction factor, or None. Cheap enough to call per request."""
    try:
        return report(con, t0, t1)["factor"]
    except Exception:
        return None
