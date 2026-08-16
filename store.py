"""SQLite storage for Windmill AC telemetry.

Three tables:

  readings   every sampled datastream value, written only when it *changes*
             (plus a keyframe every KEYFRAME_S so gaps stay bounded). AC state
             is mostly static, so this cuts a year of 30s polling from
             hundreds of millions of rows to a few million.

  hourly     precomputed per-unit energy per hour. Month and year queries read
             this instead of integrating raw samples, so they stay instant.

  exports    cache of Blynk CSV export fetches, keyed by unit/datastream/period,
             so a range already pulled is never re-fetched against the rate limit.

Readings are stored sparsely, so "the value at time T" is the most recent row
at or before T. Series reconstruction below carries values forward accordingly.
"""
import sqlite3
import time
import pathlib

import config

DB = config.ROOT / "windmill.db"
KEYFRAME_S = 900        # force-write every pin at least this often
STALE_S = 3600          # a value older than this is not carried forward

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
  unit TEXT NOT NULL, pin TEXT NOT NULL, ts INTEGER NOT NULL, val REAL,
  PRIMARY KEY (unit, pin, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS readings_ts ON readings(ts);

CREATE TABLE IF NOT EXISTS hourly (
  unit TEXT NOT NULL, hour INTEGER NOT NULL,
  wh REAL, cooling_s REAL, on_s REAL, w_avg REAL, w_max REAL, samples INTEGER,
  PRIMARY KEY (unit, hour)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS exports (
  unit TEXT NOT NULL, dsid INTEGER NOT NULL, period TEXT NOT NULL,
  name TEXT, rows INTEGER, fetched_at INTEGER,
  PRIMARY KEY (unit, dsid, period)
);

CREATE TABLE IF NOT EXISTS devices (
  unit TEXT PRIMARY KEY, device_id INTEGER, activated_at INTEGER, seen_at INTEGER
);

CREATE TABLE IF NOT EXISTS weather (
  day TEXT PRIMARY KEY, mean_f REAL, max_f REAL
);

CREATE TABLE IF NOT EXISTS weather_hourly (
  hour INTEGER PRIMARY KEY, temp_f REAL
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(hourly)")}
    if "estimated" not in cols:
        # 0 = measured, 1 = reconstructed by gapfill.py
        con.execute("ALTER TABLE hourly ADD COLUMN estimated INTEGER DEFAULT 0")
        con.commit()
    if "temp_f" not in cols:
        con.execute("ALTER TABLE hourly ADD COLUMN temp_f REAL")
        con.commit()
    return con


# ---------------------------------------------------------------- writing

_last = {}   # (unit, pin) -> (ts, val), in-memory dedupe for write-on-change


def _prime(con):
    """Seed the dedupe cache from the newest row per unit/pin."""
    for r in con.execute(
        "SELECT unit, pin, MAX(ts) ts, val FROM readings GROUP BY unit, pin"
    ):
        _last[(r["unit"], r["pin"])] = (r["ts"], r["val"])


def write_sample(con, ts, units):
    """Persist one poll. `units` is {name: pins dict or None}."""
    if not _last:
        _prime(con)
    rows = []
    for unit, pins in units.items():
        if not isinstance(pins, dict):
            continue
        for pin, raw in pins.items():
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            prev = _last.get((unit, pin))
            if prev and prev[1] == val and ts - prev[0] < KEYFRAME_S:
                continue                      # unchanged and recently written
            rows.append((unit, pin, ts, val))
            _last[(unit, pin)] = (ts, val)
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO readings(unit,pin,ts,val) VALUES (?,?,?,?)", rows)
        con.commit()
    return len(rows)


# ---------------------------------------------------------------- reading


def series(con, unit, pin, t0, t1):
    """[(ts, val)] across [t0,t1], including the last value before t0 so the
    series has a defined starting level rather than beginning at the first change."""
    pre = con.execute(
        "SELECT ts, val FROM readings WHERE unit=? AND pin=? AND ts<? "
        "ORDER BY ts DESC LIMIT 1", (unit, pin, t0)).fetchone()
    rows = con.execute(
        "SELECT ts, val FROM readings WHERE unit=? AND pin=? AND ts>=? AND ts<=? "
        "ORDER BY ts", (unit, pin, t0, t1)).fetchall()
    out = [(r["ts"], r["val"]) for r in rows]
    if pre and (not out or out[0][0] > t0) and t0 - pre["ts"] < STALE_S:
        out.insert(0, (t0, pre["val"]))
    return out


def latest(con, unit, pin, not_before=None):
    r = con.execute(
        "SELECT ts, val FROM readings WHERE unit=? AND pin=? ORDER BY ts DESC LIMIT 1",
        (unit, pin)).fetchone()
    if not r or (not_before and r["ts"] < not_before):
        return None
    return r["val"]


def bounds(con):
    r = con.execute("SELECT MIN(ts) a, MAX(ts) b FROM readings").fetchone()
    return (r["a"], r["b"]) if r and r["a"] else (None, None)


# ---------------------------------------------------------------- rollups


def hour_of(ts):
    return int(ts) // 3600 * 3600


def rebuild_hours(con, hours, power_pin, compressor_w, max_gap):
    """Recompute the hourly rollup for the given hour-start timestamps."""
    for h in sorted(set(hours)):
        for unit in [d["name"] for d in config.DEVICES]:
            pts = series(con, unit, power_pin, h, h + 3600)
            on_pts = dict(series(con, unit, "v0", h, h + 3600))
            if not pts:
                continue
            temps = [v for _, v in series(con, unit, "v1", h, h + 3600)]
            temp_f = (sum(temps) / len(temps)) if temps else None
            wh = cooling = on_s = 0.0
            peak = 0.0
            prev_t = prev_v = None
            on_state = None
            for ts, val in pts:
                peak = max(peak, val)
                if ts in on_pts:
                    on_state = on_pts[ts]
                if prev_t is not None:
                    dt = min(ts - prev_t, max_gap)
                    if dt > 0:
                        mean = (val + prev_v) / 2
                        wh += mean * dt / 3600
                        if mean > compressor_w:
                            cooling += dt
                        if on_state:
                            on_s += dt
                prev_t, prev_v = ts, val
            # carry the final level to the end of the hour
            if prev_t is not None and h + 3600 - prev_t > 0:
                dt = min(h + 3600 - prev_t, max_gap)
                wh += prev_v * dt / 3600
                if prev_v > compressor_w:
                    cooling += dt
                if on_state:
                    on_s += dt
            span = max(1.0, min(3600, (pts[-1][0] - pts[0][0]) or 3600))
            con.execute(
                "INSERT OR REPLACE INTO hourly"
                "(unit,hour,wh,cooling_s,on_s,w_avg,w_max,samples,temp_f) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (unit, h, wh, cooling, on_s, wh * 3600 / span, peak, len(pts), temp_f))
    con.commit()


def rollup_range(con, t0, t1):
    """Per-unit totals over [t0,t1) straight from the hourly table."""
    rows = con.execute(
        "SELECT unit, SUM(wh) wh, SUM(cooling_s) cooling_s, SUM(on_s) on_s, "
        "MAX(w_max) w_max, SUM(CASE WHEN estimated THEN wh ELSE 0 END) est_wh "
        "FROM hourly WHERE hour>=? AND hour<? GROUP BY unit",
        (hour_of(t0), t1)).fetchall()
    return {r["unit"]: dict(r) for r in rows}


def rollup_buckets(con, t0, t1, bucket_s):
    """[(bucket_start, {unit: wh})] for charting a long range."""
    rows = con.execute(
        "SELECT unit, hour, wh FROM hourly WHERE hour>=? AND hour<? ORDER BY hour",
        (hour_of(t0), t1)).fetchall()
    out = {}
    for r in rows:
        b = int(r["hour"]) // bucket_s * bucket_s
        out.setdefault(b, {})[r["unit"]] = out.setdefault(b, {}).get(r["unit"], 0) + (r["wh"] or 0)
    return sorted(out.items())


def dirty_hours(con, since):
    rows = con.execute(
        "SELECT DISTINCT ts/3600*3600 h FROM readings WHERE ts>=?", (since,)).fetchall()
    return [int(r["h"]) for r in rows]


def save_device(con, unit, device_id, activated_at):
    con.execute("INSERT OR REPLACE INTO devices(unit,device_id,activated_at,seen_at) "
                "VALUES (?,?,?,?)", (unit, device_id, activated_at, int(time.time())))
    con.commit()


def activations(con):
    """{unit: activation unix ts}. Empty until device info has been fetched."""
    return {r["unit"]: r["activated_at"] for r in
            con.execute("SELECT unit, activated_at FROM devices WHERE activated_at")}


def covered_hours(con, unit, t0, t1):
    r = con.execute("SELECT COUNT(*) n FROM hourly WHERE unit=? AND hour>=? AND hour<?",
                    (unit, hour_of(t0), t1)).fetchone()
    return r["n"] if r else 0


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)", (k, str(v)))
    con.commit()


def get_meta(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default
