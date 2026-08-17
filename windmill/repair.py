#!/usr/bin/env python3
"""Find holes in the history and close them, preferring real data every time.

    python3 repair.py             # report and fix
    python3 repair.py --dry-run   # report only

Holes appear for two different reasons and they need different treatment:

  the export quota was exhausted when we asked
      The data exists on Windmill's side, we just could not fetch it. Retrying
      later recovers it exactly. Never estimate this -- wait.

  the unit stopped reporting while still running
      No amount of retrying will produce it; the cloud never received it. Only
      the unit's own cumulative runtime counter can recover it, which is what
      gapfill.py does.

So this always tries a real fetch first and only reconstructs what a fetch
cannot return. Run it on a schedule -- serve.py does, hourly -- and gaps close
by themselves as quotas reset and units reconnect.
"""
import argparse
import time

from . import backfill, config, gapfill, store



def gaps(con, unit, until=None):
    """Hours missing for `unit` since it was activated, as (start, end) runs."""
    until = until or time.time()
    have = {r["hour"] for r in con.execute(
        "SELECT hour FROM hourly WHERE unit=?", (unit,))}
    house = [r["hour"] for r in con.execute("SELECT DISTINCT hour FROM hourly")]
    if not house:
        return []
    activated = store.activations(con).get(unit)
    start = store.hour_of(max(min(house), activated or min(house)))
    missing = [h for h in range(start, store.hour_of(until), 3600) if h not in have]
    if not missing:
        return []
    runs, lo, prev = [], missing[0], missing[0]
    for h in missing[1:]:
        if h == prev + 3600:
            prev = h
            continue
        runs.append((lo, prev))
        lo = prev = h
    runs.append((lo, prev))
    return runs


def repair_unit(con, device, dry_run=False):
    unit = device["name"]
    before = gaps(con, unit)
    if not before:
        return {"unit": unit, "status": "complete", "recovered": 0, "estimated": 0}

    missing_before = sum((b - a) // 3600 + 1 for a, b in before)
    recovered = estimated = 0
    notes, methods = [], {}

    # 1. real data first. Not forced: backfill's own 6h refresh window keeps a
    # unit with a long-standing gap from spending its whole daily quota retrying.
    if not dry_run:
        r = backfill.fetch_power(con, device)
        if r["status"] in ("quota", "error"):
            notes.append(f"fetch {r['status']}: {r.get('detail', '')[:60]}")
        backfill.fetch_temp(con, device)

    # 2. reconstruct what a fetch cannot return, strongest method first
    claimed = set()
    if gaps(con, unit):
        try:
            g = gapfill.fill(con, unit, dry_run=dry_run)
            if g["status"] in ("filled", "would fill"):
                claimed = set(g.get("touched", ()))
                estimated += g["hours"]
                methods.update(g.get("methods", {}))
                notes.append(f"{g['kwh']} kWh ({', '.join(
                    f'{v}h {k}' for k, v in g.get('methods', {}).items())})")
            else:
                notes.append(g["status"])
        except Exception as exc:
            notes.append(f"runtime estimate unavailable: {str(exc)[:40]}")

    # 3. last resort: no runtime counter reachable at all, so borrow from the
    #    units installed alongside this one. Weakest estimate, replaced first.
    if gaps(con, unit):
        c = gapfill.cohort_fill(con, unit, dry_run=dry_run, exclude=claimed)
        if c["status"] in ("filled", "would fill") and c["hours"]:
            estimated += c["hours"]
            methods["cohort"] = c["hours"]
            notes.append(f"{c['kwh']} kWh from {', '.join(c['peers'])}")

    after = sum((b - a) // 3600 + 1 for a, b in gaps(con, unit))
    # closed by a real fetch = everything closed that was not reconstructed
    recovered = max(0, missing_before - after - estimated)
    return {"unit": unit, "status": "repaired" if missing_before != after else "unchanged",
            "missing_before": missing_before, "missing_after": after,
            "recovered": recovered, "estimated": estimated, "methods": methods,
            "note": "; ".join(n for n in notes if n)}


def run(con, dry_run=False):
    out = []
    for device in config.DEVICES:
        out.append(repair_unit(con, device, dry_run))
        time.sleep(1.0)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    con = store.connect()
    print(f"{'unit':<20}{'before':>7}{'after':>7}{'real':>6}{'est':>5}  note")
    for r in run(con, args.dry_run):
        print(f"{r['unit']:<20}{r.get('missing_before',0):>7}{r.get('missing_after',0):>7}"
              f"{r['recovered']:>6}{r['estimated']:>5}  {r.get('note','')}")


if __name__ == "__main__":
    main()
