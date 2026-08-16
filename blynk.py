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
"""
import concurrent.futures
import json
import urllib.request

import config

MODES = {0: "Fan", 1: "Cool", 2: "Eco"}
FANS = {0: "Auto", 1: "Low", 2: "Medium", 3: "High"}

POWER, TEMP, TARGET, MODE, FAN, WATTS = "v0", "v1", "v2", "v3", "v4", "v15"


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


def read_all():
    """{device name: pins or None} for every configured device, in parallel."""
    with concurrent.futures.ThreadPoolExecutor(len(config.DEVICES)) as ex:
        pins = ex.map(read, config.DEVICES)
        return {d["name"]: p for d, p in zip(config.DEVICES, pins)}


def device_info(device, timeout=20):
    """Device metadata, including when the unit was first activated.

    Not a report export, so this is not subject to the 72/day export quota.
    """
    url = f"https://{config.HOST}/external/api/device?token={device['token']}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            j = json.load(r)
        return {"device_id": j.get("id"),
                "activated_at": int((j.get("activatedAt") or 0) / 1000) or None}
    except Exception:
        return None


def describe(pins):
    """Human-readable state from a raw pin dict."""
    if not pins:
        return {"online": False, "on": False, "mode": "-", "fan": "-",
                "temp": None, "target": None, "watts": 0}
    return {
        "online": True,
        "on": num(pins.get(POWER)) == 1,
        "mode": MODES.get(int(num(pins.get(MODE))), "?"),
        "fan": FANS.get(int(num(pins.get(FAN))), "?"),
        "temp": num(pins.get(TEMP)),
        "target": num(pins.get(TARGET)),
        "watts": num(pins.get(WATTS)),
    }
