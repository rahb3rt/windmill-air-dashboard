"""Work out which virtual pin a named feature lives on, by watching it move.

Windmill's export API names its datastreams -- "Night Mode", "Beeping Mode" --
but the live `getAll` returns bare pins, and the two numbering schemes do not
line up. Most pins were bound by correlating exported values against live reads.
That only works for a value that varies; a feature sitting at 0 on every unit,
which is what Night Mode and Beeping Mode do here, is invisible that way.

So this binds them the other way round: take a snapshot, have someone toggle the
feature in the Windmill app, and see which pin moved. One change is usually
enough to be conclusive, because the unidentified pins are otherwise static --
but the run keeps watching and reports every pin that moved, so a coincidence
shows up as an ambiguity rather than a confident wrong answer.

Deliberately read-only. The alternative -- writing to unknown pins to see what
happens -- pokes undocumented firmware on real hardware, and a wrong guess there
is not a bad reading but a physical effect nobody asked for.
"""
import json
import time

from . import blynk, config, store


#: Pins already accounted for. Anything else is fair game for identification.
KNOWN = {"v0", "v1", "v2", "v3", "v4", "v7", "v11", "v15", "v16", "v30", "v31",
         blynk.LINK}

#: Streams named by the export API that have no live pin yet.
WANTED = ("Night Mode", "Beeping Mode")


def snapshot(device):
    """{pin: value} for the pins that are candidates for identification."""
    pins = blynk.read_one(device) or {}
    return {p: blynk.num(v, None) for p, v in pins.items() if p not in KNOWN}


def start(con, unit, label):
    """Record a baseline for `unit` and start watching."""
    device = next((d for d in config.DEVICES if d["name"] == unit), None)
    if not device:
        raise ValueError(f"no unit named {unit!r}")
    base = snapshot(device)
    if not base:
        raise RuntimeError("that unit reported no unidentified pins")
    store.set_meta(con, "identify", json.dumps({
        "unit": unit, "label": label, "started": int(time.time()), "base": base,
    }))
    return {"unit": unit, "label": label, "watching": sorted(base),
            "baseline": base}


def state(con):
    try:
        raw = store.get_meta(con, "identify")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def check(con):
    """Which candidate pins have moved since the baseline."""
    st = state(con)
    if not st:
        return None
    device = next((d for d in config.DEVICES if d["name"] == st["unit"]), None)
    if not device:
        return None
    now = snapshot(device)
    moved = {p: {"from": st["base"].get(p), "to": now.get(p)}
             for p in set(st["base"]) | set(now)
             if st["base"].get(p) != now.get(p)}
    return {
        "unit": st["unit"], "label": st["label"],
        "started": st["started"],
        "elapsed": int(time.time() - st["started"]),
        "watching": sorted(st["base"]),
        "moved": moved,
        "verdict": ("waiting" if not moved
                    else "found" if len(moved) == 1
                    else "ambiguous"),
        "detail": ("Toggle it in the Windmill app now; nothing has moved yet."
                   if not moved else
                   f"{list(moved)[0].upper()} moved "
                   f"{moved[list(moved)[0]]['from']} → {moved[list(moved)[0]]['to']}."
                   if len(moved) == 1 else
                   f"{len(moved)} pins moved at once ({', '.join(sorted(moved))}), "
                   f"so this is not conclusive. Stop, wait for things to settle, "
                   f"and try again."),
    }


def confirm(con, pin):
    """Record a binding that was established by watching."""
    st = state(con)
    if not st:
        raise RuntimeError("nothing is being identified")
    bindings = known_bindings(con)
    bindings[st["label"]] = pin
    store.set_meta(con, "pin_bindings", json.dumps(bindings))
    store.set_meta(con, "identify", "")
    return bindings


def known_bindings(con):
    try:
        raw = store.get_meta(con, "pin_bindings")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def stop(con):
    store.set_meta(con, "identify", "")
