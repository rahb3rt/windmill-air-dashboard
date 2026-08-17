"""Seed history from Windmill's own export API, and cache it permanently.

Blynk exposes a CSV export per datastream. It is the only way to get data from
before this tool started logging, and it is strictly rate limited:

    72 report exports per device per day

so every fetch is recorded in the `exports` table and never repeated for a
period that is already stored. Results land in the `hourly` rollup, which is
what long-range queries read.

What is actually available:

    period=day    granularityType=MINUTE   ~1 day at 1-minute resolution
    period=week   granularityType=MINUTE   ~5 days at 1-minute resolution
    period=month  granularityType=HOURLY   30 days at 1-hour resolution

There is no `year` period -- it returns HTTP 400. Thirty days is the hard
ceiling on backfill; anything longer has to accumulate locally from here on.

Datastream IDs are NOT virtual pin numbers. The mapping below was established
by exporting each stream and correlating its values against live pin reads:

    id 1  Power Switch             -> v0
    id 2  Room Temperature         -> v1
    id 3  Set Temperature          -> v2
    id 7  Power (watts)            -> v15   [confirmed by value correlation]
    id 8  Night Mode               -> not bound to a known pin
    id 9  Filter Runtime (hours)   -> v7
    id 11 Beeping Mode             -> not bound to a known pin
    id 12 Filter Change Indicator  -> v11
    id 14 WiFi RSSI                -> v30
    id 15 Protocol Failure Count   -> v31

Every name above was read from the header of that stream's own CSV export.
An earlier version of this list had id 11 as WiFi RSSI; it is Beeping Mode,
and RSSI is id 14. Nothing read the wrong stream -- only ids 2, 7 and 9 are
ever fetched -- but the note was wrong.
"""
import io
import json
import time
import urllib.error
import urllib.request
import zipfile

from . import config, store


POWER_DSID = 7
TEMP_DSID = 2          # "Room Temperature"
DAILY_QUOTA = 72
REFRESH_S = 6 * 3600      # don't re-pull the same period more often than this


def _quota_used(con, unit):
    day_start = time.time() - 86400
    r = con.execute(
        "SELECT COUNT(*) n FROM exports WHERE unit=? AND fetched_at>?",
        (unit, day_start)).fetchone()
    return r["n"] if r else 0


class QuotaExhausted(RuntimeError):
    """Windmill refused because the device is out of report exports today."""


def _download(token, dsid, period, gran):
    url = (f"https://{config.HOST}/external/api/data/get?token={token}"
           f"&period={period}&granularityType={gran}&dataStreamId={dsid}")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as exc:
        # A quota refusal and a bad request both come back as 400; only the
        # body tells them apart, and they mean opposite things -- one is worth
        # retrying in an hour, the other never is.
        body = ""
        try:
            body = exc.read().decode()[:300]
        except Exception:
            pass
        if "limit reached" in body.lower() or "72 reports" in body:
            raise QuotaExhausted(body) from None
        raise RuntimeError(f"HTTP {exc.code}: {body or exc.reason}") from None
    if "link" not in payload:
        raise RuntimeError(payload.get("error", {}).get("message", "no export link"))
    with urllib.request.urlopen(payload["link"], timeout=120) as r:
        blob = r.read()
    z = zipfile.ZipFile(io.BytesIO(blob))
    csv = next(n for n in z.namelist() if n.endswith("_data.csv"))
    lines = z.read(csv).decode().strip().splitlines()
    name = lines[0].split(",", 1)[1] if "," in lines[0] else "?"
    points = []
    for line in lines[1:]:
        ts, _, val = line.partition(",")
        try:
            points.append((int(ts), float(val)))
        except ValueError:
            continue
    return name, sorted(points)


#: Streams worth pulling into the readings table under their live pin, so the
#: rest of the tool sees them exactly as if it had logged them itself.
IMPORTABLE = {
    12: ("v11", "Filter Change Indicator"),
    9:  ("v7",  "Filter Runtime"),
}


def fetch_stream(con, device, dsid, period="month", gran="HOURLY", force=False):
    """Import one datastream's history into `readings` under its live pin.

    Stored as changes only, matching how the sampler writes: a filter indicator
    that reads 1 for thirty days is two rows, not forty thousand. Existing rows
    are never overwritten -- locally sampled data is at 30-second resolution and
    an hourly export must not coarsen it.
    """
    unit = device["name"]
    pin, expect = IMPORTABLE[dsid]
    row = con.execute(
        "SELECT fetched_at, rows FROM exports WHERE unit=? AND dsid=? AND period=?",
        (unit, dsid, period)).fetchone()
    if row and not force and time.time() - row["fetched_at"] < REFRESH_S:
        return {"unit": unit, "stream": expect, "status": "cached", "rows": row["rows"]}

    used = _quota_used(con, unit)
    if used >= DAILY_QUOTA:
        return {"unit": unit, "stream": expect, "status": "quota", "rows": 0,
                "detail": f"{used}/{DAILY_QUOTA} exports used in last 24h"}
    try:
        name, points = _download(device["token"], dsid, period, gran)
    except QuotaExhausted:
        return {"unit": unit, "stream": expect, "status": "quota", "rows": 0,
                "detail": "Windmill's 72-exports-per-day limit; retries hourly."}
    except Exception as exc:
        return {"unit": unit, "stream": expect, "status": "error", "rows": 0,
                "detail": str(exc)}
    if expect.lower() not in (name or "").lower():
        return {"unit": unit, "stream": expect, "status": "wrong stream", "rows": 0,
                "detail": f"dataStreamId {dsid} is {name!r}, not {expect!r}"}

    have = {r["ts"] for r in con.execute(
        "SELECT ts FROM readings WHERE unit=? AND pin=?", (unit, pin))}
    added, prev = 0, None
    for ts, val in points:
        if prev is not None and val == prev:
            continue                       # write-on-change, like the sampler
        prev = val
        if ts in have:
            continue
        con.execute("INSERT OR IGNORE INTO readings(unit,pin,ts,val) VALUES (?,?,?,?)",
                    (unit, pin, int(ts), float(val)))
        added += 1
    con.execute("INSERT OR REPLACE INTO exports(unit,dsid,period,name,rows,fetched_at) "
                "VALUES (?,?,?,?,?,?)",
                (unit, dsid, period, name, len(points), int(time.time())))
    con.commit()
    return {"unit": unit, "stream": name, "status": "ok", "rows": added,
            "detail": f"{len(points)} exported points, {added} new rows"}


def fetch_filter_history(con, devices=None, force=False):
    """Pull filter indicator and runtime history for every unit.

    Two exports per unit against a 72/day quota. Worth it once: it is the only
    way to see filter changes from before this tool was logging, and the only
    way to learn the runtime at which the indicator actually trips.
    """
    out = []
    for device in (devices or config.DEVICES):
        for dsid in (12, 9):
            out.append(fetch_stream(con, device, dsid, force=force))
    return out


def fetch_power(con, device, period="month", gran="HOURLY", force=False):
    """Pull one unit's Power history into the hourly rollup. Returns a status dict."""
    unit = device["name"]
    row = con.execute(
        "SELECT fetched_at, rows FROM exports WHERE unit=? AND dsid=? AND period=?",
        (unit, POWER_DSID, period)).fetchone()
    if row and not force and time.time() - row["fetched_at"] < REFRESH_S:
        return {"unit": unit, "status": "cached", "rows": row["rows"]}

    used = _quota_used(con, unit)
    if used >= DAILY_QUOTA:
        return {"unit": unit, "status": "quota", "rows": 0,
                "detail": f"{used}/{DAILY_QUOTA} exports used in last 24h"}

    try:
        name, points = _download(device["token"], POWER_DSID, period, gran)
    except Exception as exc:
        return {"unit": unit, "status": "error", "rows": 0, "detail": str(exc)}

    # Hourly rows are per-hour mean watts, so mean * 1h is exactly that hour's Wh.
    written = 0
    for ts, watts in points:
        hour = store.hour_of(ts)
        existing = con.execute(
            "SELECT samples FROM hourly WHERE unit=? AND hour=?", (unit, hour)).fetchone()
        if existing and existing["samples"]:
            continue                     # locally sampled data is higher fidelity; keep it
        con.execute(
            "INSERT OR REPLACE INTO hourly"
            "(unit,hour,wh,cooling_s,on_s,w_avg,w_max,samples) VALUES (?,?,?,?,?,?,?,0)",
            (unit, hour, watts, 3600.0 if watts > 100 else 0.0,
             3600.0 if watts > 5 else 0.0, watts, watts))
        written += 1
    con.execute(
        "INSERT OR REPLACE INTO exports(unit,dsid,period,name,rows,fetched_at) "
        "VALUES (?,?,?,?,?,?)",
        (unit, POWER_DSID, period, name, len(points), int(time.time())))
    con.commit()
    return {"unit": unit, "status": "ok", "rows": written, "points": len(points)}


def fetch_temp(con, device, period="month", gran="HOURLY", force=False):
    """Pull one unit's Room Temperature history into the hourly rollup."""
    unit = device["name"]
    row = con.execute(
        "SELECT fetched_at FROM exports WHERE unit=? AND dsid=? AND period=?",
        (unit, TEMP_DSID, period)).fetchone()
    if row and not force and time.time() - row["fetched_at"] < REFRESH_S:
        return {"unit": unit, "status": "cached", "rows": 0}
    if _quota_used(con, unit) >= DAILY_QUOTA:
        return {"unit": unit, "status": "quota", "rows": 0}
    try:
        name, points = _download(device["token"], TEMP_DSID, period, gran)
    except Exception as exc:
        return {"unit": unit, "status": "error", "rows": 0, "detail": str(exc)}
    written = 0
    for ts, temp in points:
        hour = store.hour_of(ts)
        # only fill hours that exist and lack a temperature; never overwrite
        # a value derived from local sampling
        cur = con.execute("SELECT temp_f, samples FROM hourly WHERE unit=? AND hour=?",
                          (unit, hour)).fetchone()
        if cur is None:
            continue
        if cur["temp_f"] is not None and cur["samples"]:
            continue
        con.execute("UPDATE hourly SET temp_f=? WHERE unit=? AND hour=?",
                    (temp, unit, hour))
        written += 1
    con.execute(
        "INSERT OR REPLACE INTO exports(unit,dsid,period,name,rows,fetched_at) "
        "VALUES (?,?,?,?,?,?)",
        (unit, TEMP_DSID, period, name, len(points), int(time.time())))
    con.commit()
    return {"unit": unit, "status": "ok", "rows": written}


def run(con, period="month", force=False):
    results = []
    for device in config.DEVICES:
        results.append(fetch_power(con, device, period=period, force=force))
        time.sleep(1.5)
        t = fetch_temp(con, device, period=period, force=force)
        results.append({**t, "kind": "temp"})
        time.sleep(1.5)                  # be gentle with the export endpoint
    return results


if __name__ == "__main__":
    con = store.connect()
    for r in run(con):
        detail = f"  ({r.get('detail')})" if r.get("detail") else ""
        print(f"{r['unit']:<20} {r['status']:<7} {r['rows']:>5} hours{detail}")
