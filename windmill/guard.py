"""Be the thermostat that three of these units no longer have.

`overrun.py` finds units cooling with nothing asking them to. This module is
what to do about it once the answer is "nothing you can send it will fix this".

The evidence for that answer, measured on this system: Living Room wastes
5.01 kWh/day overcooling (56% of everything it uses), Study 2.85
(45%), Bedroom 2.04 (45%), while the healthy units waste essentially nothing.
Resyncing does not help -- the watchdog already gives up on these three after
its attempt limit -- and the split tracks pin `v113`, which reads 3 on exactly
the three bad units and 0 on the good ones. A per-unit constant that no write
changes is firmware, not a state anything here can talk out of it.

So the unit's own thermostat is treated as broken hardware and this process
supplies one: when a guarded room sits MARGIN_F below its setpoint with the
compressor running, the unit is switched off; when the room drifts back up, it
is switched on again with its settings restored. That is the same control loop
the unit should be running internally, just closed over the network at poll
cadence instead of in its firmware.

Switching a compressor from software is the part that can do real damage, so
the loop is built to refuse rather than to act:

  short-cycling  -- a compressor restarted too soon after stopping stalls
                    against its own head pressure. MIN_OFF_S/MIN_ON_S gate both
                    directions, so no amount of temperature noise can bounce a
                    unit faster than that.

  oscillation    -- off at `target - MARGIN_F` and on at `target + ON_MARGIN_F`
                    are deliberately different numbers. A single threshold
                    would switch the unit on and off around it forever.

  bad evidence   -- a frozen reading looks exactly like a steady one (see the
                    `blynk` docstring on why the cloud answers for units that
                    are not there). Acting on stale data means switching a unit
                    off on the strength of a temperature from an hour ago, so
                    link state, reading age and the resync settle window are all
                    checks that must pass before anything happens.

Decisions and actions are separate on purpose: `decide`/`report`/`pending`
only read the database and return what they would do and why, and `apply`/`run`
are the only things that touch a unit. The whole judgement is therefore
testable against a scratch database with no hardware anywhere near it.
"""
import json
import time

from . import blynk, config, energy, store


#: Below setpoint by this much means nothing asked for cooling. Same number as
#: `overrun.MARGIN_F` deliberately: the condition that flags a unit and the
#: condition that acts on it should not be able to disagree.
MARGIN_F = 2.0

#: ...and back on at this much *above* setpoint. Non-zero so the off and on
#: thresholds are different numbers -- with both at the setpoint, a room
#: hovering there would be switched off and on at every poll. 0.5 F is one step
#: of the reported temperature, the smallest gap that is actually a gap.
ON_MARGIN_F = 0.5

#: A stopped compressor must not be restarted until its refrigerant pressures
#: have equalised, or it stalls against its own head pressure. Five minutes is
#: the usual figure for room air conditioners, and it is also roughly ten poll
#: intervals, so nothing measured here can move faster than the guard reacts.
MIN_OFF_S = 300

#: The same wait in the other direction: having switched a unit on, leave it on
#: for this long before switching it off again. Without it, a unit restored into
#: a room that is still cold would be turned straight back off.
MIN_ON_S = 300

#: How old the newest row for a pin may be before its value is not evidence.
#: Readings are written on change plus a keyframe every `store.KEYFRAME_S`
#: (900 s), so on a healthy unit nothing should ever be older than that; beyond
#: a keyframe interval plus slack, the sampler itself has stopped.
FRESH_S = 1200

#: How long after switching a unit the guard stops believing what the unit says
#: about its own power state. A write takes a moment to reach the hardware and
#: another poll to come back, so a `power 1` reading from that window is the
#: state being replaced, not somebody overriding the guard. Three sample
#: intervals, matching how the watchdog judges the freshness of a link reading.
ACT_GRACE_S = 90

#: The unit must also have *pushed* something recently. `blynk.HEARTBEAT` (RSSI)
#: is the only pin that moves on its own regardless of what the AC is doing, and
#: the longest gap seen between changes on a healthy unit is ~25 min. Matched to
#: the watchdog's own wedged threshold so the two can never disagree about
#: whether a unit is reporting.
QUIET_S = 3600

#: Settings restored when a guarded unit is switched back on. Power is not one
#: of them -- it is what the restore is for -- and everything else comes from a
#: snapshot taken at switch-off rather than from a default.
RESTORE_PINS = tuple(p for p in blynk.WRITABLE if p != blynk.POWER)

# The two thresholds crossing (or meeting) would make the loop oscillate, which
# is the one failure mode that damages hardware quietly. Checked at import so a
# bad edit cannot ship as a subtle runtime behaviour.
#: Come back on after this long regardless of what the sensor says.
#:
#: The temperature route home assumes `v1` tracks the room. It does not always:
#: watched live, a unit switched off at 70F reported 64F within three minutes
#: while drawing 0 W -- that is the sensor sitting in cold air at the coil with
#: no fan moving it, not a room that fell six degrees. Waiting for such a
#: reading to climb past the setpoint could leave a room uncooled for a long
#: time, so there is a second way back that does not depend on it at all.
#:
#: Twenty minutes is a compromise: long enough that switching off was worth
#: doing, short enough that a genuinely warm room is not abandoned. If the unit
#: really is still overcooling when it comes back, the ordinary check switches
#: it off again after MIN_ON_S, so the worst case is a bounded cycle rather
#: than a room left to bake.
MAX_OFF_S = 1200

#: How long to keep an eye on a unit after switching it back on. These units
#: come up in their own default and may drift off the restored settings for a
#: few minutes; within this window the watchdog corrects it and the sampler does
#: not mistake the boot state for a preference.
SETTLE_AFTER_ON = 300

assert MARGIN_F + ON_MARGIN_F > 0, "off and on thresholds must differ"
assert MAX_OFF_S > MIN_OFF_S, "the fallback must outlast the compressor rest"


# ---------------------------------------------------------------- opt-in state
#
# All of this lives in the `meta` key/value table rather than a table of its
# own: it is a handful of scalars per unit, and a schema change would have to be
# migrated onto a database that already holds a year of history.

def _key(kind, unit):
    return f"guard:{kind}:{unit}"


def guarding_enabled(con):
    """The master switch, which is two switches.

    `config.GUARD_ENABLED` is the deployment saying it wants software control of
    a compressor at all, and defaults off; the meta flag is the dashboard's own
    pause button, so stopping the guard in a hurry does not mean editing .env
    and restarting. Same shape as the watchdog's auto-resync switches.
    """
    return config.GUARD_ENABLED and store.get_meta(con, "auto_guard", "1") == "1"


def is_enabled(con, unit):
    """Whether this unit is opted in to being guarded. Off unless asked for."""
    return store.get_meta(con, _key("enabled", unit), "0") == "1"


def set_enabled(con, unit, on):
    """Opt a unit in or out. Disabling never leaves a unit switched off: the
    caller is told whether a hold is outstanding so it can put the unit back."""
    store.set_meta(con, _key("enabled", unit), "1" if on else "0")
    return {"unit": unit, "enabled": bool(on), "held": held(con, unit)}


def enabled_units(con):
    """Configured units currently opted in, in configured order."""
    return [d["name"] for d in config.DEVICES if is_enabled(con, d["name"])]


def held(con, unit):
    """Whether the guard is the reason this unit is off right now."""
    return store.get_meta(con, _key("held", unit), "0") == "1"


def _stamp(con, kind, unit):
    try:
        return int(store.get_meta(con, _key(kind, unit), 0) or 0)
    except (TypeError, ValueError):
        return 0


def last_switch(con, unit):
    """(when the guard last switched this unit off, when it last switched it on).

    Zero means never, which lets a first decision act immediately -- a process
    that has just started has not cycled anything, so there is no cycle to
    protect.
    """
    return _stamp(con, "off_at", unit), _stamp(con, "on_at", unit)


def saved_settings(con, unit):
    """The settings snapshotted when this unit was switched off, {pin: value}.

    Kept here rather than read from `unit_settings` at restore time because the
    sampler records what a connected unit reports as its desired state, and a
    unit the guard is holding off reports power 0. Restoring from a snapshot
    taken *before* the guard touched anything cannot inherit the guard's own
    doing.
    """
    try:
        raw = json.loads(store.get_meta(con, _key("saved", unit), "") or "{}")
    except ValueError:
        return {}
    return {p: float(v) for p, v in raw.items()
            if p in RESTORE_PINS and v is not None}


# ---------------------------------------------------------------- deciding


def _recent(con, unit, pin, now, max_age=FRESH_S):
    """(value, age) for a pin, or (None, None) if there is nothing recent."""
    r = con.execute(
        "SELECT ts, val FROM readings WHERE unit=? AND pin=? ORDER BY ts DESC LIMIT 1",
        (unit, pin)).fetchone()
    if not r or r["val"] is None or now - r["ts"] > max_age:
        return None, None
    return r["val"], now - r["ts"]


def decide(con, unit, now=None, link=None, settling=None):
    """What the guard would do to one unit, and why. Reads only.

    `link` (1/0/None) and `settling` (bool) are parameters rather than lookups
    so a caller that already knows can pass what it has, and so the refusals can
    be tested without manufacturing the state that causes them. Left at None,
    both are read from the database.

    The returned dict always carries `reason` (a stable code) and `detail` (a
    sentence), for the acting case and the not-acting case alike: "nothing
    happened" is only trustworthy if it comes with the reason nothing happened.
    """
    now = int(now or time.time())
    is_held = held(con, unit)
    d = {"unit": unit, "action": None, "held": is_held,
         "enabled": is_enabled(con, unit), "temp": None, "target": None,
         "watts": None, "power": None, "off_at": None, "on_at": None,
         "ready_at": None}

    def out(reason, detail, **extra):
        return {**d, "reason": reason, "detail": detail, **extra}

    if not d["enabled"]:
        return out("disabled", "Guarding is switched off for this unit.")
    if not guarding_enabled(con):
        return out("off", "The thermostat guard is switched off for this system.")

    if settling is None:
        settling = store.settling_until(con, unit, now) is not None
    if settling:
        # A unit reports a transient state for minutes after a resync. Judging
        # it on that would mean acting on settings nobody chose.
        return out("settling", "Mid-resync; its readings are not trustworthy yet.")

    if link is None:
        link, _ = _recent(con, unit, blynk.LINK, now)
    if link is None:
        return out("no-link-data", "No recent record of whether it is connected.")
    if not link:
        # Writes to a unit with no session are stored by the cloud and never
        # delivered, so "switched off" would be a lie told to the audit trail.
        return out("no-session", "No session with the cloud, so nothing sent "
                                 "would reach it.")

    beat = store.last_change(con, unit, blynk.HEARTBEAT, window=QUIET_S * 2, now=now)
    if beat is None or now - beat > QUIET_S:
        return out("not-reporting",
                   "Connected but nothing pushed for "
                   f"{'ever' if beat is None else str((now - beat) // 60) + ' min'}; "
                   "its readings may be frozen.")

    temp, _ = _recent(con, unit, blynk.TEMP, now)
    target, _ = _recent(con, unit, blynk.TARGET, now)
    watts, _ = _recent(con, unit, config.POWER_PIN, now)
    power, power_age = _recent(con, unit, blynk.POWER, now)
    missing = [n for n, v in (("temperature", temp), ("setpoint", target),
                              ("power draw", watts), ("on/off", power))
               if v is None]
    if missing:
        return out("stale", f"No reading for {', '.join(missing)} in the last "
                            f"{FRESH_S // 60} min.")

    off_at, on_at = target - MARGIN_F, target + ON_MARGIN_F
    d.update({"temp": temp, "target": target, "watts": watts,
              "power": power, "off_at": off_at, "on_at": on_at})
    last_off, last_on = last_switch(con, unit)

    if is_held:
        # Only a report that post-dates the guard's own write, by enough for the
        # unit to have acted on it, says anything about what someone else did.
        # Without that window the guard reads the `power 1` still in flight as
        # an override and stands down from the switch-off it just made.
        power_at = now - power_age          # _recent gives an age; this is when
        if power and power_at > last_off + ACT_GRACE_S:
            # Someone turned it back on by hand or through the app. Whoever did
            # that outranks the guard, so the hold is dropped rather than
            # re-asserted -- a dashboard that silently undoes what you just did
            # is the behaviour people hate in thermostats.
            return out("released", "Someone switched it back on, so the guard "
                                   "is standing down.", action="release")
        # The fallback outranks the temperature, because the whole point of it
        # is to not trust the temperature.
        if last_off and now - last_off >= MAX_OFF_S:
            return out("max-off-time",
                       f"Off for {(now - last_off) // 60} min. Switching back on "
                       f"without waiting for {on_at:.1f}F -- the reading is "
                       f"{temp:.0f}F, and a sensor that disagrees with the room "
                       f"is not a reason to leave it off indefinitely.",
                       action="on")
        if temp < on_at:
            left = MAX_OFF_S - (now - last_off) if last_off else MAX_OFF_S
            return out("holding",
                       f"Room is {temp:.0f}F against a {target:.0f}F setpoint; "
                       f"holding it off until {on_at:.1f}F, or "
                       f"{max(0, left) // 60} min from now, whichever comes first.",
                       ready_at=(last_off + MAX_OFF_S) if last_off else None)
        wait = MIN_OFF_S - (now - last_off)
        if last_off and wait > 0:
            return out("min-off-time",
                       f"Room is back to {temp:.0f}F, but it was switched off "
                       f"{(now - last_off) // 60} min ago -- waiting out the "
                       f"{MIN_OFF_S // 60} min compressor rest.",
                       ready_at=last_off + MIN_OFF_S)
        return out("back-to-setpoint",
                   f"Room is {temp:.0f}F, at or above its {target:.0f}F setpoint; "
                   "switching it back on.", action="on")

    if not power:
        return out("already-off", "The unit is off; nothing to guard against.")
    if temp > off_at:
        return out("in-band",
                   f"Room is {temp:.0f}F against a {target:.0f}F setpoint; "
                   f"nothing to do above {off_at:.1f}F.")
    if watts <= energy.COMPRESSOR_W:
        # Below setpoint but not actually cooling: the fan alone costs little
        # and switching the unit off would not save it.
        return out("not-cooling",
                   f"Room is {temp:.0f}F against a {target:.0f}F setpoint, but "
                   f"it is only drawing {watts:.0f}W -- the compressor is idle.")
    wait = MIN_ON_S - (now - last_on)
    if last_on and wait > 0:
        return out("min-on-time",
                   f"Overcooling, but it was switched on {(now - last_on) // 60} "
                   f"min ago -- leaving it running for the {MIN_ON_S // 60} min "
                   "minimum.",
                   ready_at=last_on + MIN_ON_S)
    return out("overcooling",
               f"Running at {watts:.0f}W with the room at {temp:.0f}F, "
               f"{target - temp:.1f}F below its {target:.0f}F setpoint; "
               "switching it off.", action="off")


def report(con, now=None):
    """{unit: decision} for every configured unit, guarded or not."""
    now = int(now or time.time())
    return {d["name"]: decide(con, d["name"], now) for d in config.DEVICES}


def pending(con, now=None):
    """Just the units something should actually happen to."""
    return {u: d for u, d in report(con, now).items() if d["action"]}


# ---------------------------------------------------------------- acting


def _snapshot(con, unit, now=None):
    """The settings to restore this unit to, taken before switching it off.

    Whatever the unit is reporting now, falling back to the stored settings for
    anything it has not reported. Both can be empty on a unit never seen
    healthy, in which case the restore is a plain power-on and the unit keeps
    what it already had.
    """
    stored = store.get_settings(con, unit)
    now = int(now or time.time())
    out = {}
    for pin in RESTORE_PINS:
        val, _ = _recent(con, unit, pin, now)
        if val is None:
            val = stored.get(pin)
        if val is not None:
            out[pin] = float(val)
    return out


def _device(unit):
    return next((d for d in config.DEVICES if d["name"] == unit), None)


def apply(con, decision, control, record=None, now=None):
    """Carry out one decision. `control(device, field, value)` does the writing.

    Passing the writer in is what keeps the hardware out of the decision path:
    the caller hands over `blynk.control` in production and a stub in a test,
    and this function cannot tell the difference.

    Bookkeeping is written only when the write is accepted. A refused switch-off
    that still recorded a hold would leave the guard convinced it had stopped a
    unit that is merrily still running.
    """
    now = int(now or time.time())
    unit = decision["unit"]
    action = decision.get("action")
    base = {"unit": unit, "action": action, "reason": decision.get("reason"),
            "detail": decision.get("detail"), "results": []}
    if not action:
        return {**base, "ok": True, "changed": False}

    if action == "release":
        # Bookkeeping only: the unit is already in the state we want it in.
        store.set_meta(con, _key("held", unit), "0")
        result = {**base, "ok": True, "changed": True}
        if record:
            record(decision, result)
        return result

    device = _device(unit)
    if not device:
        return {**base, "ok": False, "changed": False,
                "detail": f"{unit} is not a configured device."}

    results = []
    if action == "off":
        # Snapshotted first: once power is 0, what the unit reports is the
        # guard's own doing and no longer evidence of what anyone wanted.
        saved = _snapshot(con, unit, now)
        r = control(device, "power", 0)
        results.append(r)
        if r.get("ok"):
            store.set_meta(con, _key("saved", unit), json.dumps(saved, sort_keys=True))
            store.set_meta(con, _key("held", unit), "1")
            store.set_meta(con, _key("off_at", unit), now)
    else:
        # Switching on is "restore these settings and turn it on", which is
        # exactly what blynk.apply_settings does -- including re-sending once it
        # is powered, the step that stops these units coming back on Eco/Low.
        # `verify` is off because this runs inside the watchdog loop and the
        # settle window below already arranges for it to be re-checked.
        saved = {**saved_settings(con, unit), blynk.POWER: 1.0}
        r = blynk.apply_settings(device, saved, verify=False)
        results.append(r)
        if r.get("ok") or r.get("written"):
            store.set_meta(con, _key("held", unit), "0")
            store.set_meta(con, _key("on_at", unit), now)
            # Watch it for a few minutes: if it drifts off these settings
            # anyway, the watchdog's restore pass puts it back. Marking the
            # window also stops the sampler recording the boot state as though
            # it were a preference.
            store.mark_settling(con, unit, now + SETTLE_AFTER_ON)

    ok = bool(r.get("ok"))
    detail = decision.get("detail")
    if not ok:
        detail = (f"Could not switch it {action}: "
                  f"{r.get('detail') or r.get('reason') or 'the write failed'}")
    elif action == "on":
        detail = f"{decision.get('detail')} Restored {blynk.settings_label(saved)}."
    result = {**base, "ok": ok, "changed": ok, "detail": detail,
              "results": results}
    if record:
        record(decision, result)
    return result


def run(con, control, now=None, record=None):
    """Decide for every unit and act on the ones that need it. {unit: result}."""
    now = int(now or time.time())
    return {unit: apply(con, d, control, record=record, now=now)
            for unit, d in pending(con, now).items()}
