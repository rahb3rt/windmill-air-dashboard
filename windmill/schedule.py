"""Time-of-day setpoints.

A rule says: at this local time, on these days, set this field to this value
for this scope. Scope is a unit name, `floor:<name>`, or empty for everything.

Two decisions worth stating.

**Rules fire, they do not hold.** A rule is applied once when its time passes
and then leaves the unit alone, so changing the temperature by hand afterwards
sticks until the next rule. The alternative -- continuously enforcing the
schedule -- means the dashboard silently undoing what someone just did, which is
the behaviour people hate in thermostats.

**A missed rule is skipped, not caught up.** If the process is down at 07:00 and
starts at 11:00, applying the 07:00 rule then would be acting on an intention
that expired hours ago. Only rules whose time has passed *within the last few
minutes* run, so a restart never replays the day.

Local time throughout, including across daylight saving: rules are stored as
minutes past midnight and compared against local wall-clock, so "07:00" stays
07:00 rather than drifting by an hour twice a year.
"""
import time

from . import blynk, config, store


#: How late a rule may fire. Wide enough to survive a slow tick or a reload,
#: narrow enough that a restart hours later does not replay the morning.
GRACE_S = 300

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def targets(con, scope):
    """Unit names a scope refers to."""
    names = [d["name"] for d in config.DEVICES]
    if not scope:
        return names
    if scope.startswith("floor:"):
        return store.units_on_floor(con, scope[len("floor:"):])
    return [scope] if scope in names else []


def describe(rule, con=None):
    """A rule in words: 'weekdays 07:00 — set target 68°F on floor 2'."""
    hh, mm = divmod(int(rule["at_min"]), 60)
    days = rule["days"] or "0123456"
    when = ("every day" if days == "0123456"
            else "weekdays" if days == "01234"
            else "weekends" if days == "56"
            else " ".join(DAY_NAMES[int(d)] for d in days if d.isdigit()))
    field, val = rule["field"], rule["value"]
    what = (f"{val:.0f}°F" if field == "target"
            else blynk.MODES.get(int(val), val) if field == "mode"
            else blynk.FANS.get(int(val), val) if field == "fan"
            else ("on" if val else "off") if field == "power" else val)
    where = ("every unit" if not rule["scope"]
             else rule["scope"][len("floor:"):] + " floor"
             if rule["scope"].startswith("floor:") else rule["scope"])
    return f"{when} {hh:02d}:{mm:02d} — set {field} {what} on {where}"


def due(con, now=None, grace=GRACE_S):
    """Rules whose moment has just passed and that have not run for it yet."""
    now = now or time.time()
    lt = time.localtime(now)
    minute_now = lt.tm_hour * 60 + lt.tm_min
    today = str(lt.tm_wday)
    midnight = now - (minute_now * 60 + lt.tm_sec)

    out = []
    for rule in store.schedules(con, enabled_only=True):
        if today not in (rule["days"] or "0123456"):
            continue
        fire_at = midnight + rule["at_min"] * 60
        if not (0 <= now - fire_at <= grace):
            continue
        # last_run is a timestamp, so a rule that already fired for *this*
        # occurrence is skipped while the same rule tomorrow is not.
        if rule["last_run"] and rule["last_run"] >= fire_at:
            continue
        out.append(rule)
    return out


def apply(con, rule, control, record=None, now=None):
    """Run one rule. `control` is called per unit and returns a result dict."""
    now = now or time.time()
    units = targets(con, rule["scope"])
    results = {}
    for name in units:
        device = next((d for d in config.DEVICES if d["name"] == name), None)
        if not device:
            continue
        try:
            results[name] = control(device, rule["field"], rule["value"])
        except Exception as exc:
            results[name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    store.update_schedule(con, rule["id"], last_run=int(now))
    ok = [n for n, r in results.items() if r.get("ok")]
    bad = {n: r.get("detail") or r.get("reason") for n, r in results.items()
           if not r.get("ok")}
    if record:
        record(rule, results, ok, bad)
    return {"rule": rule["id"], "applied": sorted(ok), "failed": bad,
            "units": len(units)}


def next_runs(con, now=None, limit=5):
    """The next few times something will happen, for the page."""
    now = now or time.time()
    lt = time.localtime(now)
    minute_now = lt.tm_hour * 60 + lt.tm_min
    midnight = now - (minute_now * 60 + lt.tm_sec)
    out = []
    for rule in store.schedules(con, enabled_only=True):
        days = rule["days"] or "0123456"
        for ahead in range(8):
            day = (lt.tm_wday + ahead) % 7
            if str(day) not in days:
                continue
            when = midnight + ahead * 86400 + rule["at_min"] * 60
            if when > now:
                out.append((when, rule))
                break
    out.sort(key=lambda x: x[0])
    return [{"at": int(w), "id": r["id"], "text": describe(r, con)}
            for w, r in out[:limit]]
