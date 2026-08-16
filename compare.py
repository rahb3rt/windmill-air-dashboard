#!/usr/bin/env python3
"""Print the before/after comparison that the dashboard shows.

    python3 compare.py
    python3 compare.py --change 2026-08-08 --new "Kitchen,Living Room,Spare Room"

Defaults come from WINDMILL_CHANGE_DATE and WINDMILL_NEW_UNITS in .env. All the
maths lives in analysis.py so this and the page can never disagree.
"""
import argparse

import analysis
import store


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--change", help="YYYY-MM-DD the new units came online")
    p.add_argument("--new", help="comma-separated names of the added units")
    p.add_argument("--settle", type=int, default=2,
                   help="days to discard after the change as install noise")
    args = p.parse_args()

    con = store.connect()
    c = analysis.comparison(
        con, change=args.change,
        new_units=args.new.split(",") if args.new else None,
        settle=args.settle)

    if not c["available"]:
        raise SystemExit(c["reason"])

    money = lambda v: f"{'-' if v < 0 else '+'}${abs(v):,.2f}"
    b, a = c["before"], c["after"]
    print(f"{len(c['new_units'])} units added {c['change']} "
          f"({', '.join(c['new_units'])}); "
          f"{c['change']}..{c['resume']} discarded as install noise\n")
    print(f"BEFORE  n={b['n']:<3} kWh = {b['a']:6.2f} + {b['b']:5.2f} x CDD  "
          f"R2={b['r2']:.2f}  observed {65+b['cdd_lo']:.1f}-{65+b['cdd_hi']:.1f}F")
    print(f"AFTER   n={a['n']:<3} kWh = {a['a']:6.2f} + {a['b']:5.2f} x CDD  "
          f"R2={a['r2']:.2f}  observed {65+a['cdd_lo']:.1f}-{65+a['cdd_hi']:.1f}F")

    print(f"\nAt ${c['rate']:.3f}/kWh, each degree warmer narrows the gap by "
          f"${c['per_degree']:.3f}/day\n")
    gap = (c["climate"] or {}).get("high_gap", 7.0)
    print(f"{'mean F':>7}{'~high':>7}{'before':>8}{'after':>14}"
          f"{'extra/day':>20}{'extra/month':>22}")
    for r in c["rows"]:
        after = (f"{r['after_lo']:.1f}" if r["after_hi"] == r["after_lo"]
                 else f"{r['after_lo']:.1f}-{r['after_hi']:.1f}")
        d = (f"{money(r['delta_lo']*c['rate'])}" if r["delta_hi"] == r["delta_lo"]
             else f"{money(r['delta_lo']*c['rate'])} to {money(r['delta_hi']*c['rate'])}")
        m = (f"{money(r['delta_lo']*c['rate']*30)}" if r["delta_hi"] == r["delta_lo"]
             else f"{money(r['delta_lo']*c['rate']*30)} to {money(r['delta_hi']*c['rate']*30)}")
        print(f"{r['mean_f']:>7.0f}{r['mean_f']+gap:>7.0f}{r['before']:>8.1f}"
              f"{after:>14}{d:>20}{m:>22}")

    if c["breakeven_lo"]:
        be = (f"{c['breakeven_lo']:.1f}F" if not c["breakeven_hi"]
              or abs(c["breakeven_hi"] - c["breakeven_lo"]) < 0.1
              else f"{c['breakeven_lo']:.1f}-{c['breakeven_hi']:.1f}F")
        print(f"\nBreak-even at a daily mean of {be} (a high near "
              f"{c['breakeven_lo']+gap:.0f}F).")
    else:
        print("\nNo break-even: the newer setup is cheaper at every temperature.")

    pb = c.get("payback")
    if pb:
        print()
        if pb["possible"]:
            print(f"Hot days needed to break even: {pb['days_needed']:.1f} per season "
                  f"at {pb['at_f']:.1f}F; your summers deliver {pb['days_available']:.1f}.")
        else:
            print(f"Hot days needed to break even: never. Even at {pb['at_f']:.1f}F, the "
                  f"hottest daily mean on record, it costs "
                  f"{pb['delta_at_hottest']:+.1f} kWh/day more.")
        print(f"  Cost per cooling season: ${pb['season_deficit_cost']:.0f} "
              f"({pb['season_deficit_kwh']:.0f} kWh).")

    cl = c["climate"]
    if cl:
        print(f"  In {', '.join(cl['seasons'])} local summers that happened "
              f"{cl['days_above_be']:.1f}x per season "
              f"(hottest daily mean on record {cl['hottest_mean']:.1f}F).")
        print(f"  Net effect over a full cooling season: {money(cl['season_cost'])}.")

    if c["extrapolated"]:
        print(f"\n  ! Break-even is extrapolated past the hottest day in the fit "
              f"({c['observed_hi_f']:.1f}F). Read it as 'well beyond anything observed'.")
    if c["imputed"]:
        print(f"  ! {', '.join(c['imputed'])} lacks history here; imputed at "
              f"{c['impute_band'][0]:.1f}-{c['impute_band'][1]:.1f} kWh/day from siblings "
              f"-- that is the range shown above.")
    if c.get("estimate_heavy"):
        print(f"  ! {c['est_share_after']:.0f}% of the AFTER period and "
              f"{c['est_share_before']:.0f}% of BEFORE is reconstructed, not measured. "
              f"Re-run once real data replaces it.")
    if c["weak"]:
        print(f"  ! Thin fit (n={b['n']}/{a['n']}). Directional only; re-run as days accumulate.")


if __name__ == "__main__":
    main()
