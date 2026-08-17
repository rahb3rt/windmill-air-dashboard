"""Detect units cooling when nothing asked them to.

The symptom this exists for: a unit runs its compressor hard while the room is
already colder than its setpoint. It is not a comfort problem you notice --
the room feels fine -- it is a unit that has stopped acting on its own
thermostat, and it shows up only as an electricity bill.

Two tests, because one alone is not enough:

  overcooling  -- of the time this unit spent running, how much of it was spent
                  with the room already MARGIN_F below its setpoint. This is the
                  primary test and it stands on its own, which matters because a
                  whole floor can be misbehaving at once (peer comparison sees
                  nothing wrong when every peer is equally wrong).

  peer duty    -- how hard this unit ran compared to the other units on its
                  floor over the same window. Catches a unit working much harder
                  than its neighbours for conditions they all share, and gives
                  the overcooling number context.

Measured against this system's own history, the split is not subtle: units in
good health spend 0-1% of their running time more than 2 F below setpoint,
while the misbehaving ones spend 46-58%. Thresholds sit in the wide gap between
those, not near either.
"""
import statistics

from . import blynk, config, energy, store


MARGIN_F = 2.0          # this far below setpoint means nothing asked for cooling
WINDOW_S = 3600         # judge on this much recent history
MIN_RUN_S = 900         # below this much running time there is nothing to judge
OVERCOOL_FRAC = 0.5     # this share of running time overcooling -> flagged
PEER_RATIO = 1.6        # ...or running this many times the floor's median duty
MIN_PEERS = 2           # peer test needs at least this many others on the floor
MAX_GAP = 900           # never attribute more than this across a hole in data


def assess(con, unit, t0, t1):
    """How hard `unit` ran over [t0,t1], and how much of it was uncalled for."""
    watts = store.series(con, unit, config.POWER_PIN, t0, t1)
    if len(watts) < 2:
        return None
    temp = store.step_fn(store.series(con, unit, blynk.TEMP, t0, t1), default=None)
    tgt = store.step_fn(store.series(con, unit, blynk.TARGET, t0, t1), default=None)
    live = store.step_fn(store.series(con, unit, blynk.LINK, t0, t1), default=1)

    ran = overcool = covered = 0.0
    deltas = []
    prev = None
    for ts, w in watts:
        if prev is not None and live(prev[0]):
            dt = min(ts - prev[0], MAX_GAP)
            if dt > 0:
                covered += dt
                if (w + prev[1]) / 2 > energy.COMPRESSOR_W:
                    ran += dt
                    a, b = temp(prev[0]), tgt(prev[0])
                    if a is not None and b is not None:
                        deltas.append(a - b)
                        if a <= b - MARGIN_F:
                            overcool += dt
        prev = (ts, w)

    if covered <= 0:
        return None
    return {
        "unit": unit,
        "ran_s": round(ran),
        "overcool_s": round(overcool),
        "covered_s": round(covered),
        "duty": round(ran / covered, 3),
        "overcool_frac": round(overcool / ran, 3) if ran else 0.0,
        "mean_delta": round(statistics.fmean(deltas), 2) if deltas else None,
    }


def last_fix_map(con, action="overrun-resync"):
    """{unit: when it was last resynced for overrunning}."""
    return {r["unit"]: r["t"] for r in con.execute(
        "SELECT unit, MAX(ts) t FROM sync_events WHERE action=? GROUP BY unit",
        (action,)) if r["t"]}


def report(con, now, window=WINDOW_S, since_fix=True):
    """Assess every unit, compare each against its floor, and flag the outliers.

    Returns {unit: assessment} where each assessment carries a `verdict` of
    ok | overcooling | working-harder, plus the floor context behind it.

    With `since_fix`, a unit that was recently resynced is judged only on what
    it has done *since* -- otherwise the window still contains the misbehaviour
    that prompted the resync, and the watchdog would grade its own fix on
    evidence predating it and resync again for no reason. MIN_RUN_S then does
    the rest of the work: a unit that has not run enough since the fix to be
    judged is left alone rather than assumed guilty.
    """
    t1 = now
    fixes = last_fix_map(con) if since_fix else {}
    floors = store.get_floors(con)
    rows = {}
    for device in config.DEVICES:
        name = device["name"]
        t0 = max(now - window, fixes.get(name, 0))
        a = assess(con, name, t0, t1)
        if a:
            a["floor"] = floors.get(name)
            a["since_fix"] = name in fixes and t0 > now - window
            rows[name] = a

    for name, a in rows.items():
        peers = [b for n, b in rows.items()
                 if n != name and a["floor"] and b["floor"] == a["floor"]]
        med = statistics.median([p["duty"] for p in peers]) if peers else None
        a["peers"] = len(peers)
        a["floor_duty"] = round(med, 3) if med is not None else None

        # Not enough running time to say anything either way.
        if a["ran_s"] < MIN_RUN_S:
            a["verdict"] = "ok"
            a["detail"] = ("Not enough running time since its last resync to "
                           "judge yet." if a.get("since_fix")
                           else "Barely ran; nothing to judge.")
            continue

        if a["overcool_frac"] >= OVERCOOL_FRAC:
            a["verdict"] = "overcooling"
            a["detail"] = (
                f"Ran {a['ran_s']//60} min, {round(a['overcool_frac']*100)}% of it "
                f"with the room already {MARGIN_F:.0f}F or more below its setpoint.")
        elif (len(peers) >= MIN_PEERS and med and med > 0
              and a["duty"] >= med * PEER_RATIO):
            a["verdict"] = "working-harder"
            a["detail"] = (
                f"Ran {round(a['duty']*100)}% of the last hour against "
                f"{round(med*100)}% for the rest of floor {a['floor']}.")
        else:
            a["verdict"] = "ok"
            a["detail"] = f"Ran {a['ran_s']//60} min, in line with demand."
    return rows


def flagged(con, now, window=WINDOW_S):
    """Just the units judged to be running when they should not be."""
    return {n: a for n, a in report(con, now, window).items()
            if a["verdict"] != "ok"}
