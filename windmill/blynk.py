"""Thin client for the Blynk HTTP API that backs dashboard.windmillair.com.

Datastream map (V0-V4 confirmed against the community Home Assistant
integration, github.com/bzellman/WindmillAC):

    V0  power        0 = off, 1 = on
    V1  current temperature (deg F)
    V2  target temperature (deg F)
    V3  mode         0 = fan only, 1 = cool, 2 = eco
    V4  fan speed    0 = auto, 1 = low, 2 = medium, 3 = high

Everything else is undocumented and was inferred by observation. V15 tracks
instantaneous watts: it rises and falls rather than accumulating, sits near
zero on idle units, and jumps to a few hundred when a compressor engages.
Treat the rest as unlabelled -- `windmill.py pins` dumps them raw.

`getAll` answers out of the *cloud's* copy of each datastream, which outlives
the device's session with it. A unit that has dropped off WiFi therefore still
returns a complete, plausible pin dict -- last known values, frozen, with
nothing in the response to say so. `isHardwareConnected` is the only signal
that distinguishes a live unit from a stale record of one, so every poll asks
for it and carries the answer as a synthetic pin (LINK) alongside the real
ones. Downstream that pin is what `online` means, and what stops the hourly
rollup integrating across a stretch the unit was not actually reporting.
"""
import concurrent.futures
import json
import time
import urllib.parse
import urllib.request

from . import config


MODES = {0: "Fan", 1: "Cool", 2: "Eco"}
FANS = {0: "Auto", 1: "Low", 2: "Medium", 3: "High"}

POWER, TEMP, TARGET, MODE, FAN, WATTS = "v0", "v1", "v2", "v3", "v4", "v15"
FAILURES = "v31"        # cumulative protocol failures, monotonic per unit

#: Synthetic pin: 1 when the unit held a session with the cloud at poll time,
#: 0 when the cloud answered for a unit that was not there. Not a real
#: datastream -- named outside the vN space so it can never collide with one.
LINK = "c0"

#: Settings the app writes and a wedged unit fails to pick up. Re-asserting
#: these is what `resync` does.
WRITABLE = (POWER, TARGET, MODE, FAN)

#: Pins the *unit* pushes upward. These are the only honest evidence it is
#: alive: a write to any WRITABLE pin is echoed straight back by `getAll`
#: whether or not the hardware ever saw it, so reading a setpoint back proves
#: nothing.
REPORTED = (TEMP, WATTS, "v30", FAILURES)

#: Liveness heartbeat. RSSI is the only one of the above that moves on its own
#: no matter what the AC is doing -- it is radio noise, not state. The others
#: go flat for hours on a unit that is simply idle, which would make "quiet"
#: indistinguishable from "gone". Measured across this system's own history,
#: the longest gap between RSSI changes on a healthy unit was ~25 min, against
#: 37 h for room temperature and ~3 h for watts.
HEARTBEAT = ("v30",)

#: Setpoint limits, matching what the Windmill app itself allows. Writes are
#: clamped rather than rejected so a held-down button stops at the limit.
TARGET_MIN, TARGET_MAX = 60, 86

#: The state a unit should be in when nothing better is known: on, cooling, fan
#: on auto. Target is not included on purpose -- it is a per-room preference,
#: and a single default would be the wrong answer for most rooms, so whatever
#: the unit already has is kept.
DEFAULTS = {POWER: float(config.DEFAULT_POWER),
            MODE: float(config.DEFAULT_MODE),
            FAN: float(config.DEFAULT_FAN)}

#: The controls the dashboard exposes, and what each will accept. Nothing
#: outside this map is writable through the API -- an endpoint that forwarded
#: arbitrary pin writes would be a way to poke undocumented datastreams on
#: real hardware by accident.
CONTROLS = {
    "power":  {"pin": POWER,  "choices": (0, 1)},
    "target": {"pin": TARGET, "clamp": (TARGET_MIN, TARGET_MAX)},
    "mode":   {"pin": MODE,   "choices": tuple(MODES)},
    "fan":    {"pin": FAN,    "choices": tuple(FANS)},
}


def coerce(field, value):
    """Validate/clamp a control value. Returns (pin, value) or raises ValueError."""
    spec = CONTROLS.get(field)
    if not spec:
        raise ValueError(f"{field!r} is not a control")
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a number")
    if "clamp" in spec:
        lo, hi = spec["clamp"]
        v = max(lo, min(hi, v))
    elif v not in spec["choices"]:
        raise ValueError(f"{field} must be one of {list(spec['choices'])}")
    return spec["pin"], v


def control(device, field, value):
    """Apply one setting to a unit, refusing units that cannot receive it."""
    pin, v = coerce(field, value)
    if connected(device) is False:
        return {"ok": False, "reason": "no session", "field": field,
                "detail": "The unit is not connected, so the change would be "
                          "stored in the cloud and never reach it."}
    if not write(device, pin, v):
        return {"ok": False, "reason": "write refused", "field": field,
                "detail": "The cloud would not accept the write."}
    return {"ok": True, "field": field, "pin": pin, "value": v}


#: What Windmill's own API offers, as observed. There is no vendor
#: documentation for any of this -- `dashboard.windmillair.com` is a
#: white-labelled Blynk deployment, and every entry below was established by
#: calling it and watching what came back. Declared here, next to the code that
#: makes the calls, so the reference cannot drift from the client.
UPSTREAM = [
    {"path": "/external/api/getAll", "group": "Reading state", "auth": "token",
     "summary": "Every datastream's current value for one device.",
     "params": [{"name": "token", "required": True, "does": "The device's auth token."}],
     "returns": "A flat JSON object, {\"v0\": 1, \"v1\": 71, ...}.",
     "gotcha": "Answers out of the cloud's copy, which outlives the device's "
               "session with it. A unit that has dropped off still returns a "
               "complete, plausible object of frozen values, with nothing in "
               "the response to say so."},
    {"path": "/external/api/get", "group": "Reading state", "auth": "token",
     "summary": "One datastream. Append the pin as a bare parameter.",
     "params": [{"name": "token", "required": True},
                {"name": "<pin>", "required": True,
                 "does": "Valueless, e.g. ...&v2 -- not v2=."}],
     "returns": "The bare value as text, not JSON."},
    {"path": "/external/api/isHardwareConnected", "group": "Reading state",
     "auth": "token",
     "summary": "Whether the device currently holds a session with the cloud.",
     "params": [{"name": "token", "required": True}],
     "returns": "The bare text true or false.",
     "gotcha": "The only call that distinguishes a live unit from a stale "
               "record of one. Everything else answers the same either way."},
    {"path": "/external/api/device", "group": "Reading state", "auth": "token",
     "summary": "Device metadata, including when it was first activated.",
     "params": [{"name": "token", "required": True}],
     "returns": "JSON with id and activatedAt (unix milliseconds).",
     "gotcha": "Not a report export, so it does not count against the export "
               "quota below."},

    {"path": "/external/api/update", "group": "Writing state", "auth": "token",
     "summary": "Set one or more datastreams.",
     "params": [{"name": "token", "required": True},
                {"name": "<pin>", "required": True,
                 "does": "e.g. &v2=72. Several may be sent at once."}],
     "returns": "HTTP 200 with an empty body.",
     "gotcha": "Success means the cloud stored the value, not that the hardware "
               "applied it. A write to a wedged unit is accepted and never "
               "arrives, and reading it back returns what you wrote -- so a "
               "read-back can never confirm a write took."},

    {"path": "/external/api/data/get", "group": "History export", "auth": "token",
     "summary": "Request a CSV export of one datastream's history.",
     "params": [
         {"name": "token", "required": True},
         {"name": "dataStreamId", "required": True,
          "does": "NOT the virtual pin number -- see the id map below."},
         {"name": "period", "required": True, "values": "day | week | month",
          "does": "There is no year; it returns HTTP 400."},
         {"name": "granularityType", "required": True,
          "values": "MINUTE | HOURLY",
          "does": "day/week support MINUTE; month is HOURLY."}],
     "returns": "JSON containing a `link` to a ZIP. Inside it, a *_data.csv of "
                "`unix_seconds,value` rows, with the stream's name in the header.",
     "gotcha": "Limited to 72 exports per device per day. Retention is 30 days, "
               "so this is the hard ceiling on how far back history can be "
               "recovered -- anything older only exists if it was logged locally."},
]

#: Export dataStreamId -> virtual pin. Two different numbering schemes, and the
#: mapping is not published. Every *name* below was read straight from the
#: header of that stream's own CSV export, so the names are Windmill's own and
#: not inferred. The pin each one binds to was then found by correlating
#: exported values against live pin reads.
#:
#: Ids 4, 5, 6, 10, 13 and everything above 15 could not be checked -- the
#: export quota (72/device/day) was exhausted mid-survey, and a quota refusal
#: is indistinguishable from a missing stream. Treat "stops at 15" as unproven.
DSID_MAP = {
    1: ("Power Switch", "v0"), 2: ("Room Temperature", "v1"),
    3: ("Set Temperature", "v2"), 7: ("Power (watts)", "v15"),
    8: ("Night Mode", None), 9: ("Filter Runtime (hours)", "v7"),
    11: ("Beeping Mode", None), 12: ("Filter Change Indicator", "v11"),
    14: ("WiFi RSSI", "v30"), 15: ("Protocol Failure Count", "v31"),
}


def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read(device, timeout=15):
    """All datastreams for one device, or None if unreachable."""
    url = f"https://{config.HOST}/external/api/getAll?token={device['token']}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def connected(device, timeout=15):
    """True/False if the unit currently holds a session, None if unanswerable.

    None is deliberately distinct from False: an unreachable *cloud* says
    nothing about the unit, and must not be recorded as the unit being down.
    """
    url = (f"https://{config.HOST}/external/api/isHardwareConnected"
           f"?token={device['token']}")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode().strip().lower() == "true"
    except Exception:
        return None


def read_one(device):
    """Pins for one device with the link state folded in, for a single unit."""
    return _read_one(device)


def _read_one(device):
    """Pins for one device with the link state folded in as LINK."""
    pins, link = read(device), connected(device)
    if pins is None:
        return None
    if link is not None:
        pins[LINK] = 1 if link else 0
    return pins


def read_all():
    """{device name: pins or None} for every configured device, in parallel."""
    with concurrent.futures.ThreadPoolExecutor(len(config.DEVICES)) as ex:
        pins = ex.map(_read_one, config.DEVICES)
        return {d["name"]: p for d, p in zip(config.DEVICES, pins)}


def write(device, pin, value, timeout=15):
    """Set one datastream. Returns True if the cloud accepted the write.

    Acceptance means the cloud stored it, not that the unit applied it -- a
    desynced unit's writes are accepted and never arrive. Check `connected`
    first if that distinction matters.
    """
    q = urllib.parse.urlencode({"token": device["token"], pin: value})
    try:
        with urllib.request.urlopen(
                f"https://{config.HOST}/external/api/update?{q}", timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


#: pin -> the control name it belongs to, for talking about pins in English
CONTROL_OF = {spec["pin"]: name for name, spec in CONTROLS.items()}


def settings_label(saved):
    """'72°F, Cool, fan Auto, on' from a pin->value dict."""
    bits = []
    if POWER in saved:
        bits.append("on" if num(saved[POWER]) == 1 else "off")
    if TARGET in saved:
        bits.append(f"{num(saved[TARGET]):.0f}°F")
    if MODE in saved:
        bits.append(MODES.get(int(num(saved[MODE])), "?"))
    if FAN in saved:
        bits.append(f"fan {FANS.get(int(num(saved[FAN])), '?')}")
    return ", ".join(bits) or "nothing"


def apply_settings(device, wanted, power_last=True, resend=True, settle=2.0,
                   verify=False, attempts=1, writer=None):
    """Write a set of settings to a unit and make them stick.

    Three places needed this and each grew its own copy, which is how the same
    bug got fixed three times: a unit that is off drops writes aimed at it and
    boots into its own default, so settings written before power-on are lost and
    the unit comes back on Eco/Low when it was told Cool/Auto.

    The order that works, in one place:

        settings -> power -> settings again

    `power_last` puts the power write after the others so the unit comes up in
    the state it was told rather than briefly running on whatever the cloud
    still held. `resend` repeats the settings once it is on, which is the part
    that fixes the dropped writes. `verify` reads back and re-sends anything
    that did not take -- worth it for a deliberate act like a resync, and not
    for the watchdog, which is already re-checking on its own tick and should
    not block that loop sleeping.

    A caveat that cannot be engineered away: `getAll` echoes whatever was
    written, so a read-back confirms the *cloud* holds the value, not that the
    hardware applied it. Reversion does show up eventually, because these units
    push their own state upward -- which is how the Eco/Low drift was spotted at
    all -- but seconds after a write, agreement means little.
    """
    wanted = {p: float(v) for p, v in (wanted or {}).items()
              if p in WRITABLE and v is not None}
    if not wanted:
        return {"ok": True, "written": [], "missed": [], "detail": "nothing to set"}
    if connected(device) is False:
        return {"ok": False, "written": [], "missed": sorted(wanted),
                "reason": "no session",
                "detail": "The unit has no session, so nothing sent would reach it."}

    settings = {p: v for p, v in wanted.items() if p != POWER}
    written = []

    # `writer` keeps the hardware injectable: callers that already take a
    # control callable for testing hand theirs in, rather than this reaching
    # past them to the network and quietly breaking their test seam.
    def put(pin, val):
        if writer:
            return bool(writer(device, CONTROL_OF.get(pin, pin), int(val)).get("ok"))
        return write(device, pin, int(val))

    def push(group):
        for pin, val in sorted(group.items()):
            if put(pin, val):
                written.append(pin)

    if power_last and POWER in wanted:
        push(settings)
        put(POWER, wanted[POWER])
        written.append(POWER)
        if resend and num(wanted[POWER]) == 1 and settings:
            if settle:
                time.sleep(settle)
            push(settings)
    else:
        push(wanted)

    missed = []
    if verify:
        for attempt in range(attempts + 1):
            if attempt and settle:
                time.sleep(settle)
            now = read(device) or {}
            missed = [p for p, v in wanted.items() if num(now.get(p)) != num(v)]
            if not missed or attempt == attempts:
                break
            for pin in missed:
                put(pin, wanted[pin])

    names = [CONTROL_OF.get(p, p) for p in sorted(set(written))]
    return {
        "ok": not missed,
        "written": names,
        "missed": [CONTROL_OF.get(p, p) for p in missed],
        "detail": (f"set {settings_label(wanted)}" if not missed else
                   f"{', '.join(CONTROL_OF.get(p, p) for p in missed)} did not "
                   f"stick; the unit is refusing them, not failing to receive them"),
    }


def resync(device, settle=3.0, attempts=2, desired=None, record=None,
           defaults=None):
    """Power-cycle a unit's *operating state* and re-push every setting.

    This is the remote equivalent of the unplug/replug ritual for the common
    case: the unit still holds its session but its state machine has stopped
    acting on what the cloud tells it. Turning V0 off, letting it settle, then
    writing every setting back and powering on again makes the cloud re-deliver
    all of them, which a merely wedged unit will act on.

    `desired` is the state to restore, normally the caller's stored copy rather
    than whatever the cloud currently holds -- by the time a unit is wedged the
    cloud's copy is exactly the thing under suspicion. Falls back to reading the
    unit when no stored settings exist yet.

    It cannot help a unit that has actually dropped its session -- nothing sent
    reaches those, so they are refused here rather than silently no-oped, and
    still need mains power removed.
    """
    if connected(device) is False:
        return {"ok": False, "reason": "no session",
                "detail": "The unit is not connected to the cloud, so it cannot "
                          "receive anything sent to it. Only cutting mains power "
                          "will bring it back."}
    before = read(device)
    if not before:
        return {"ok": False, "reason": "unreachable",
                "detail": "Could not read the unit's current settings."}

    # Prefer the caller's stored settings; the unit's own copy is only a
    # fallback for a unit never seen healthy.
    saved = {p: float(v) for p, v in (desired or {}).items()
             if p in WRITABLE and v is not None}
    source = "stored"
    if not saved:
        # Nothing on file for this unit. Fall back to the configured defaults
        # rather than to what the unit currently reports -- a unit being
        # resynced is under suspicion, so its own copy is the one thing that
        # should not decide where it ends up. Its setpoint is kept, since that
        # is a room preference no default can stand in for.
        saved = dict(defaults or DEFAULTS)
        if TARGET not in saved and before.get(TARGET) is not None:
            saved[TARGET] = float(before[TARGET])
        source = "defaults"
    if record:
        record(saved)          # persisted before anything is touched, so an
                               # interrupted resync still leaves a record of
                               # what the unit was set to
    if POWER not in saved:
        return {"ok": False, "reason": "unsafe",
                "detail": "Could not read whether the unit was on, and guessing "
                          "risks leaving it in the wrong state.",
                "preserved": settings_label(saved)}

    # Nothing has changed yet if this write fails, so bailing here is safe.
    if not write(device, POWER, 0):
        return {"ok": False, "reason": "write refused",
                "detail": "The cloud would not accept a write to this unit.",
                "preserved": settings_label(saved)}

    time.sleep(settle)
    applied = apply_settings(device, saved, settle=settle, verify=True,
                             attempts=attempts)
    missed = [p for p in WRITABLE
              if CONTROL_OF.get(p, p) in (applied.get("missed") or [])]

    label = settings_label(saved)
    return {
        "ok": not missed,
        "reason": None if not missed else "settings not preserved",
        "preserved": label,
        "restored": dict(saved),
        "source": source,
        "missed": [CONTROL_OF.get(p, p) for p in missed],
        "failures_before": num(before.get(FAILURES)),
        "failures_after": num((read(device) or {}).get(FAILURES)),
        "detail": (f"Cycled in software; restored {label}"
                   + (" from stored settings." if source == "stored"
                      else " from the configured defaults (nothing on file yet).")
                   if not missed else
                   f"Cycled, but {', '.join(CONTROL_OF.get(p, p) for p in missed)} "
                   f"did not stick. The unit may still be wedged."),
    }


def device_info(device, timeout=20):
    """Device metadata: activation, and what firmware it is running.

    Not a report export, so this is not subject to the 72/day export quota.

    The firmware fields matter more than they look. These units do not all run
    the same build -- some are years behind -- and an OTA update that a unit was
    offline for simply never happened, silently. Recording the version is what
    makes "this unit misbehaves" checkable against "this unit missed an update".
    """
    url = f"https://{config.HOST}/external/api/device?token={device['token']}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            j = json.load(r)
        hw = j.get("hardwareInfo") or {}
        return {"device_id": j.get("id"),
                "activated_at": int((j.get("activatedAt") or 0) / 1000) or None,
                "fw_version": hw.get("version"),
                "fw_build": hw.get("build"),
                "blynk_version": hw.get("blynkVersion"),
                "board": hw.get("boardType"),
                "fw_updated_at": int((j.get("firmwareUpdatedAt") or 0) / 1000) or None}
    except Exception:
        return None


def describe(pins):
    """Human-readable state from a raw pin dict.

    `online` follows the unit's session, not the HTTP call: values still come
    back for a unit that has gone away, and reporting those as live is how a
    desync turns into plausible-looking data. When LINK is absent (the cloud
    answered but would not say) the reading is kept but flagged unverified.
    """
    if not pins:
        return {"online": False, "linked": False, "stale": True,
                "on": False, "mode": "-", "fan": "-",
                "temp": None, "target": None, "watts": 0, "failures": None}
    link = pins.get(LINK)
    linked = None if link is None else bool(link)
    return {
        # unknown link state is treated as online -- it is the pre-existing
        # behaviour, and the alternative blanks the dashboard on a cloud hiccup
        "online": linked is not False,
        "linked": linked,
        "stale": linked is False,
        "on": num(pins.get(POWER)) == 1,
        "mode": MODES.get(int(num(pins.get(MODE))), "?"),
        "fan": FANS.get(int(num(pins.get(FAN))), "?"),
        "temp": num(pins.get(TEMP)),
        "target": num(pins.get(TARGET)),
        "watts": num(pins.get(WATTS)),
        "failures": num(pins.get(FAILURES)) if FAILURES in pins else None,
    }
