"""Derive energy use from logged power samples.

The cloud exposes no trustworthy cumulative energy counter, so kWh is
integrated locally: trapezoid rule over sampled watts (V15) against sample
timestamps. Gaps longer than MAX_GAP (device offline, logger stopped) are
skipped rather than interpolated across, so downtime never invents usage.
"""
import json
import time

import blynk
import config

COMPRESSOR_W = 100   # sustained draw above this means the compressor is running
MAX_GAP = 600        # seconds; never integrate across a longer hole


def load_samples(since=None):
    if not config.SAMPLES.exists():
        return []
    rows = []
    with config.SAMPLES.open() as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is None or row.get("ts", 0) >= since:
                rows.append(row)
    rows.sort(key=lambda r: r["ts"])
    return rows


def midnight():
    return time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))


def integrate(rows, name):
    """-> (kWh, cooling seconds, powered-on seconds) for one unit."""
    wh = cooling = powered = 0.0
    prev = None
    for row in rows:
        pins = row.get("units", {}).get(name)
        if not isinstance(pins, dict) or "error" in pins:
            prev = None          # unit was unreachable; break the interval
            continue
        watts = blynk.num(pins.get(blynk.WATTS))
        on = blynk.num(pins.get(blynk.POWER)) == 1
        if prev is not None:
            dt = row["ts"] - prev[0]
            if 0 < dt <= MAX_GAP:
                mean = (watts + prev[1]) / 2
                wh += mean * dt / 3600
                if mean > COMPRESSOR_W:
                    cooling += dt
                if prev[2]:
                    powered += dt
        prev = (row["ts"], watts, on)
    return wh / 1000, cooling, powered


def summarize(live, rows):
    """Combine live pin reads with integrated history into one payload."""
    units, total_w, total_kwh = [], 0.0, 0.0
    for device in config.DEVICES:
        name = device["name"]
        pins = live.get(name)
        state = blynk.describe(pins)
        kwh, cooling, powered = integrate(rows, name)
        total_w += state["watts"]
        total_kwh += kwh
        units.append({
            **state,
            "name": name,
            "watts": round(state["watts"]),
            "kwh": round(kwh, 3),
            "cost": round(kwh * config.RATE_PER_KWH, 2),
            "cooling_min": round(cooling / 60),
            "on_min": round(powered / 60),
            "pins": pins or {},
        })

    for u in units:
        u["share"] = round(100 * u["kwh"] / total_kwh, 1) if total_kwh else 0.0

    series = {
        "t": [r["ts"] for r in rows],
        "units": {
            d["name"]: [
                round(blynk.num(r["units"].get(d["name"], {}).get(blynk.WATTS)))
                if isinstance(r["units"].get(d["name"]), dict) else None
                for r in rows
            ]
            for d in config.DEVICES
        },
    }

    return {
        "updated": int(time.time()),
        "rate": config.RATE_PER_KWH,
        "samples": len(rows),
        "since": rows[0]["ts"] if rows else None,
        "units": units,
        "totals": {
            "watts": round(total_w),
            "kwh": round(total_kwh, 3),
            "cost": round(total_kwh * config.RATE_PER_KWH, 2),
            "cooling_min": sum(u["cooling_min"] for u in units),
            "online": sum(1 for u in units if u["online"]),
            "count": len(units),
        },
        "series": series,
    }


def snapshot():
    return summarize(blynk.read_all(), load_samples(since=midnight()))
