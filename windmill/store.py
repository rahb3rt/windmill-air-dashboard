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

`readings` also carries one synthetic pin, `blynk.LINK`, recording whether the
unit held a session with the cloud at each poll. It is stored like any other
pin so history is queryable the same way, and it is what stops the rollup
carrying a frozen wattage forward across time the unit was not really there.
"""
import bisect
import sqlite3
import time
import pathlib

from . import blynk, config


DB = pathlib.Path(config.DB_PATH)
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

-- what the sync watchdog saw and did, so an automatic power cycle is never
-- something that just silently happened
CREATE TABLE IF NOT EXISTS sync_events (
  ts INTEGER NOT NULL, unit TEXT NOT NULL, state TEXT, action TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS sync_events_unit ON sync_events(unit, ts);

-- Each unit's settings as they were last seen with the unit healthy. This is
-- what a resync restores: by the time a unit is wedged, the cloud's own copy
-- is the thing under suspicion, so it cannot be the source of truth for
-- putting the unit back the way it was.
-- Every action that changed something, whoever asked for it. sync_events is
-- the watchdog's own reasoning; this is the record of things actually done, so
-- "what happened to this unit, and who did it" is one query rather than a
-- reconstruction.
CREATE TABLE IF NOT EXISTS audit (
  ts INTEGER NOT NULL, source TEXT, target TEXT, action TEXT,
  detail TEXT, ok INTEGER
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);

-- Time-of-day setpoints. A rule names what to change, what to change it to,
-- when, and on which days; `scope` is a unit name, "floor:<name>", or "" for
-- everything. Rules are applied by the scheduler, not stored as state -- the
-- unit_settings table remains the record of what a unit should currently be.
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL, field TEXT NOT NULL, value REAL NOT NULL,
  at_min INTEGER NOT NULL,          -- minutes past local midnight
  days TEXT NOT NULL DEFAULT '0123456',
  enabled INTEGER NOT NULL DEFAULT 1,
  last_run INTEGER,
  created_at INTEGER
);

-- The AC units themselves. These began life as WINDMILL_TOKEN_* lines in .env,
-- which meant adding a unit was a text-editor-and-restart job. Holding them
-- here lets the dashboard add one, and lets a unit be renamed without its
-- history being orphaned. `.env` is still read once, to seed this.
CREATE TABLE IF NOT EXISTS units (
  name TEXT PRIMARY KEY, token TEXT NOT NULL, position INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1, added_at INTEGER, source TEXT,
  label TEXT
) WITHOUT ROWID;

-- which floor each unit is on, so a whole floor can be set at once. Free text
-- rather than a number: "Basement" and "2nd" are both things a house has.
CREATE TABLE IF NOT EXISTS unit_floors (
  unit TEXT PRIMARY KEY, floor TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS unit_settings (
  unit TEXT NOT NULL, pin TEXT NOT NULL, val REAL,
  updated_at INTEGER, source TEXT,
  PRIMARY KEY (unit, pin)
) WITHOUT ROWID;

-- one row per day per metric, so a rolling estimate can be seen converging
CREATE TABLE IF NOT EXISTS estimate_log (
  day TEXT NOT NULL, key TEXT NOT NULL, value REAL, PRIMARY KEY (day, key)
);
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
    if "method" not in cols:
        # how an estimated hour was produced: runtime | idle | cohort
        con.execute("ALTER TABLE hourly ADD COLUMN method TEXT")
        con.commit()
    unit_cols = {r["name"] for r in con.execute("PRAGMA table_info(units)")}
    if unit_cols and "label" not in unit_cols:
        con.execute("ALTER TABLE units ADD COLUMN label TEXT")
        con.commit()
    dev_cols = {r["name"] for r in con.execute("PRAGMA table_info(devices)")}
    for col, kind in (("fw_version", "TEXT"), ("fw_build", "TEXT"),
                      ("blynk_version", "TEXT"), ("board", "TEXT"),
                      ("fw_updated_at", "INTEGER")):
        if col not in dev_cols:
            con.execute(f"ALTER TABLE devices ADD COLUMN {col} {kind}")
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


def step_fn(points, default=1):
    """Sparse [(ts, val)] -> f(ts) reading the value in force at ts.

    Readings are written on change, so a pin's value between two rows is the
    earlier one. `default` covers time before the first row, where the pin was
    simply never recorded -- history predating a pin must not read as zero.
    """
    if not points:
        return lambda ts: default
    stamps = [p[0] for p in points]
    vals = [p[1] for p in points]

    def at(ts):
        i = bisect.bisect_right(stamps, ts) - 1
        return default if i < 0 else vals[i]
    return at


def last_change(con, unit, pins, window=6 * 3600, now=None):
    """When any of `pins` last actually changed value, or None if never seen.

    Not the same as the newest row: a keyframe rewrites an unchanged value
    every KEYFRAME_S, so recency of rows says nothing about whether the unit is
    still reporting. Only a differing value does.
    """
    now = int(now or time.time())
    newest = None
    for pin in pins:
        rows = con.execute(
            "SELECT ts, val FROM readings WHERE unit=? AND pin=? AND ts>=? "
            "ORDER BY ts", (unit, pin, now - window)).fetchall()
        prev = None
        for r in rows:
            if prev is not None and r["val"] != prev:
                newest = r["ts"] if newest is None else max(newest, r["ts"])
            prev = r["val"]
    return newest


#: Every table that keys rows by unit name. A rename has to move all of them
#: together or the unit loses its history, which is the one thing here that
#: cannot be re-fetched.
UNIT_KEYED = (("readings", "unit"), ("hourly", "unit"), ("devices", "unit"),
              ("sync_events", "unit"), ("audit", "target"),
              ("unit_settings", "unit"), ("unit_floors", "unit"),
              ("exports", "unit"))


def units(con, enabled_only=True):
    """[{name, token}] in display order, shaped like the old config.DEVICES."""
    q = "SELECT name, token FROM units"
    if enabled_only:
        q += " WHERE enabled=1"
    return [{"name": r["name"], "token": r["token"]}
            for r in con.execute(q + " ORDER BY position, name")]


def unit_rows(con):
    """Everything about each unit except the token, which never leaves here."""
    return [{"name": r["name"], "label": r["label"] or r["name"],
             "enabled": bool(r["enabled"]),
             "added_at": r["added_at"], "source": r["source"],
             "position": r["position"],
             # Enough to tell two units apart when pasting tokens, and no
             # more: this value grants full control of an appliance.
             "token_hint": ("…" + r["token"][-4:]) if r["token"] else "missing"}
            for r in con.execute(
                "SELECT * FROM units ORDER BY position, name")]


def add_unit(con, name, token, position=None, source="dashboard"):
    name = (name or "").strip()
    token = (token or "").strip()
    if not name:
        raise ValueError("a unit needs a name")
    if len(token) < 16:
        raise ValueError("that does not look like a device token")
    if con.execute("SELECT 1 FROM units WHERE name=?", (name,)).fetchone():
        raise ValueError(f"there is already a unit called {name!r}")
    if position is None:
        r = con.execute("SELECT MAX(position) p FROM units").fetchone()
        position = ((r["p"] or 0) + 1) if r else 1
    con.execute("INSERT INTO units(name,token,position,enabled,added_at,source) "
                "VALUES (?,?,?,1,?,?)",
                (name, token, position, int(time.time()), source))
    con.commit()
    return name


def remove_unit(con, name, purge=False):
    """Forget a unit. Its history is kept unless `purge`.

    Keeping it is the default because the energy already recorded against that
    room is still true, and a unit is far more often unplugged than genuinely
    gone forever.
    """
    if not con.execute("SELECT 1 FROM units WHERE name=?", (name,)).fetchone():
        raise ValueError(f"no unit called {name!r}")
    con.execute("DELETE FROM units WHERE name=?", (name,))
    if purge:
        for table, col in UNIT_KEYED:
            con.execute(f"DELETE FROM {table} WHERE {col}=?", (name,))
    con.commit()
    return {"removed": name, "history": "deleted" if purge else "kept"}


def rename_unit(con, old, new):
    """Rename a unit and carry every row that references it.

    Done in one transaction: a half-renamed unit would read as two units, each
    with half a history.
    """
    new = (new or "").strip()
    if not new:
        raise ValueError("a unit needs a name")
    if not con.execute("SELECT 1 FROM units WHERE name=?", (old,)).fetchone():
        raise ValueError(f"no unit called {old!r}")
    if con.execute("SELECT 1 FROM units WHERE name=?", (new,)).fetchone():
        raise ValueError(f"there is already a unit called {new!r}")
    moved = 0
    with con:
        con.execute("UPDATE units SET name=? WHERE name=?", (new, old))
        for table, col in UNIT_KEYED:
            cur = con.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, old))
            moved += cur.rowcount or 0
        for key in ("guard:enabled", "guard:held", "guard:off_at", "guard:on_at",
                    "guard:saved", "settling", "alert:dropped", "alert:filter",
                    "alert:gave-up", "filter_due", "rejected"):
            con.execute("UPDATE meta SET k=? WHERE k=?",
                        (f"{key}:{new}", f"{key}:{old}"))
    return {"from": old, "to": new, "rows_moved": moved}


def labels(con):
    """{name: label}. The name is the key history is stored under; the label is
    what a person calls it. Changing a label therefore costs nothing, while
    changing a name has to move every row that references it."""
    return {r["name"]: (r["label"] or r["name"])
            for r in con.execute("SELECT name, label FROM units")}


def set_label(con, name, label):
    label = (label or "").strip() or None
    con.execute("UPDATE units SET label=? WHERE name=?", (label, name))
    con.commit()
    return label or name


def set_unit_enabled(con, name, on):
    con.execute("UPDATE units SET enabled=? WHERE name=?", (1 if on else 0, name))
    con.commit()


def seed_units(con, devices):
    """Import units defined in .env, once, so an existing setup keeps working.

    Only fills gaps: a unit already in the table is left alone, so editing .env
    later cannot silently overwrite a token changed through the dashboard.
    """
    added = []
    for i, d in enumerate(devices):
        if con.execute("SELECT 1 FROM units WHERE name=?", (d["name"],)).fetchone():
            continue
        con.execute("INSERT INTO units(name,token,position,enabled,added_at,source) "
                    "VALUES (?,?,?,1,?,?)",
                    (d["name"], d["token"], i, int(time.time()), "env"))
        added.append(d["name"])
    con.commit()
    return added


def set_floor(con, unit, floor):
    """Assign a unit to a floor, or clear it with a blank/None floor."""
    floor = (floor or "").strip()
    if floor:
        con.execute("INSERT OR REPLACE INTO unit_floors(unit,floor) VALUES (?,?)",
                    (unit, floor))
    else:
        con.execute("DELETE FROM unit_floors WHERE unit=?", (unit,))
    con.commit()
    return floor or None


def get_floors(con):
    """{unit: floor} for every unit that has been assigned one."""
    return {r["unit"]: r["floor"] for r in
            con.execute("SELECT unit, floor FROM unit_floors WHERE floor IS NOT NULL")}


def units_on_floor(con, floor):
    return [r["unit"] for r in con.execute(
        "SELECT unit FROM unit_floors WHERE floor=? ORDER BY unit", (floor,))]


def add_schedule(con, scope, field, value, at_min, days="0123456"):
    cur = con.execute(
        "INSERT INTO schedules(scope,field,value,at_min,days,enabled,created_at) "
        "VALUES (?,?,?,?,?,1,?)",
        (scope, field, float(value), int(at_min), days, int(time.time())))
    con.commit()
    return cur.lastrowid


def schedules(con, enabled_only=False):
    q = "SELECT * FROM schedules"
    if enabled_only:
        q += " WHERE enabled=1"
    return [dict(r) for r in con.execute(q + " ORDER BY at_min, id")]


def update_schedule(con, sched_id, **fields):
    allowed = {"scope", "field", "value", "at_min", "days", "enabled", "last_run"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    con.execute(f"UPDATE schedules SET {','.join(k+'=?' for k in sets)} WHERE id=?",
                (*sets.values(), int(sched_id)))
    con.commit()
    return True


def delete_schedule(con, sched_id):
    con.execute("DELETE FROM schedules WHERE id=?", (int(sched_id),))
    con.commit()


def mark_settling(con, unit, until):
    """Note that a unit is mid-resync and not to be believed until `until`.

    Kept in the database rather than in memory so it survives the auto-reload,
    and so every thread and request sees the same answer.
    """
    set_meta(con, f"settling:{unit}", int(until))


def settling_until(con, unit, now=None):
    """When this unit stops settling, or None if it is not."""
    try:
        until = int(get_meta(con, f"settling:{unit}", 0))
    except (TypeError, ValueError):
        return None
    return until if until > (now or time.time()) else None


def save_settings(con, unit, values, source, ts=None):
    """Record a unit's settings as the state to restore it to.

    `source` says where the value came from -- "dashboard" for a deliberate
    change made here, "observed" for one noticed on a healthy unit (which is
    how a change made in the Windmill app itself gets picked up).
    """
    ts = int(ts or time.time())
    rows = [(unit, pin, float(val), ts, source)
            for pin, val in values.items() if val is not None]
    if not rows:
        return 0
    con.executemany(
        "INSERT OR REPLACE INTO unit_settings(unit,pin,val,updated_at,source) "
        "VALUES (?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


def get_settings(con, unit):
    """{pin: value} last recorded for this unit, empty if never seen healthy."""
    return {r["pin"]: r["val"] for r in con.execute(
        "SELECT pin, val FROM unit_settings WHERE unit=?", (unit,))}


def settings_meta(con, unit):
    """When this unit's stored settings were last touched, and by what."""
    r = con.execute("SELECT MAX(updated_at) t, source FROM unit_settings "
                    "WHERE unit=?", (unit,)).fetchone()
    return {"updated_at": r["t"], "source": r["source"]} if r and r["t"] else {}


def log_sync(con, unit, state, action=None, detail=None, ts=None):
    con.execute("INSERT INTO sync_events(ts,unit,state,action,detail) VALUES (?,?,?,?,?)",
                (int(ts or time.time()), unit, state, action, detail))
    con.commit()


def log_audit(con, source, target, action, detail=None, ok=True, ts=None):
    """Record one thing that was actually done."""
    con.execute("INSERT INTO audit(ts,source,target,action,detail,ok) "
                "VALUES (?,?,?,?,?,?)",
                (int(ts or time.time()), source, target, action, detail,
                 1 if ok else 0))
    con.commit()


def _audit_where(source, target, since, until):
    q, args = "", []
    if source:
        q += " AND source=?"; args.append(source)
    if target:
        q += " AND target=?"; args.append(target)
    if since:
        q += " AND ts>=?"; args.append(int(since))
    if until:
        q += " AND ts<=?"; args.append(int(until))
    return q, args


def audit_events(con, source=None, target=None, since=None, until=None, limit=300):
    where, args = _audit_where(source, target, since, until)
    return [dict(r) for r in con.execute(
        "SELECT ts,source,target,action,detail,ok FROM audit WHERE 1=1" + where +
        " ORDER BY ts DESC LIMIT ?", (*args, limit))]


def count_audit(con, source=None, target=None, since=None, until=None):
    where, args = _audit_where(source, target, since, until)
    r = con.execute("SELECT COUNT(*) n FROM audit WHERE 1=1" + where, args).fetchone()
    return r["n"] if r else 0


def audit_span(con):
    r = con.execute("SELECT MIN(ts) a, MAX(ts) b FROM audit").fetchone()
    return (r["a"], r["b"]) if r and r["a"] else (None, None)


def audit_sources(con):
    return [r["source"] for r in con.execute(
        "SELECT DISTINCT source FROM audit WHERE source IS NOT NULL ORDER BY source")]


def _sync_where(unit, since, until):
    q, args = "", []
    if unit:
        q += " AND unit=?"; args.append(unit)
    if since:
        q += " AND ts>=?"; args.append(int(since))
    if until:
        q += " AND ts<=?"; args.append(int(until))
    return q, args


def sync_events(con, unit=None, since=None, until=None, limit=50):
    where, args = _sync_where(unit, since, until)
    return [dict(r) for r in con.execute(
        "SELECT ts,unit,state,action,detail FROM sync_events WHERE 1=1" + where +
        " ORDER BY ts DESC LIMIT ?", (*args, limit))]


def count_sync_events(con, unit=None, since=None, until=None):
    """How many events match, so the page can say when it is showing a subset."""
    where, args = _sync_where(unit, since, until)
    r = con.execute("SELECT COUNT(*) n FROM sync_events WHERE 1=1" + where,
                    args).fetchone()
    return r["n"] if r else 0


def sync_events_span(con):
    """(first, last) event timestamps, for an 'everything' range."""
    r = con.execute("SELECT MIN(ts) a, MAX(ts) b FROM sync_events").fetchone()
    return (r["a"], r["b"]) if r and r["a"] else (None, None)


def count_actions(con, unit, actions, since):
    """How many times these actions were taken on a unit since `since`."""
    marks = ",".join("?" * len(actions))
    r = con.execute(
        f"SELECT COUNT(*) n FROM sync_events WHERE unit=? AND action IN ({marks}) "
        f"AND ts>=?", (unit, *actions, int(since))).fetchone()
    return r["n"] if r else 0


def attempts_since_recovery(con, unit, actions):
    """How many times these actions were taken since the unit last recovered.

    Kept in the database rather than in the watchdog's memory so that restarting
    the process -- which auto-reload does on every code change -- cannot reset a
    give-up counter and let a unit be power-cycled indefinitely.
    """
    r = con.execute("SELECT MAX(ts) t FROM sync_events WHERE unit=? AND action='recovered'",
                    (unit,)).fetchone()
    since = (r["t"] if r and r["t"] else 0)
    marks = ",".join("?" * len(actions))
    n = con.execute(
        f"SELECT COUNT(*) n FROM sync_events WHERE unit=? AND action IN ({marks}) "
        f"AND ts>?", (unit, *actions, since)).fetchone()
    return n["n"] if n else 0


def note_recovery(con, unit, detail=None):
    """Mark a unit healthy again, resetting its attempt counter. No-op if the
    last thing recorded for it was already a recovery."""
    r = con.execute("SELECT action FROM sync_events WHERE unit=? ORDER BY ts DESC LIMIT 1",
                    (unit,)).fetchone()
    if r and r["action"] == "recovered":
        return False
    if not r:
        return False          # never had a problem; nothing to reset
    log_sync(con, unit, "ok", "recovered", detail or "Back to normal.")
    return True


def last_action(con, unit, action):
    """Timestamp of this unit's most recent event with the given action."""
    r = con.execute("SELECT MAX(ts) t FROM sync_events WHERE unit=? AND action=?",
                    (unit, action)).fetchone()
    return r["t"] if r and r["t"] else None


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
            # Watts keep arriving for a unit that has dropped its session --
            # frozen at whatever it last reported. Integrating those invents
            # energy, so a stretch with no session is treated as a hole and
            # left for gapfill to reconstruct honestly.
            live = step_fn(series(con, unit, blynk.LINK, h, h + 3600), default=1)
            temps = [v for _, v in series(con, unit, "v1", h, h + 3600)]
            temp_f = (sum(temps) / len(temps)) if temps else None
            wh = cooling = on_s = measured = 0.0
            peak = 0.0
            prev_t = prev_v = None
            on_state = None
            for ts, val in pts:
                peak = max(peak, val)
                if ts in on_pts:
                    on_state = on_pts[ts]
                if prev_t is not None and live(prev_t):
                    dt = min(ts - prev_t, max_gap)
                    if dt > 0:
                        # Hold the earlier value, matching how readings are
                        # written and how series() reconstructs them. Averaging
                        # the two draws a ramp the unit never reported; see
                        # energy.integrate_points for what that cost.
                        wh += prev_v * dt / 3600
                        measured += dt
                        if prev_v > compressor_w:
                            cooling += dt
                        if on_state:
                            on_s += dt
                prev_t, prev_v = ts, val
            # carry the final level to the end of the hour
            if prev_t is not None and h + 3600 - prev_t > 0 and live(prev_t):
                dt = min(h + 3600 - prev_t, max_gap)
                wh += prev_v * dt / 3600
                measured += dt
                if prev_v > compressor_w:
                    cooling += dt
                if on_state:
                    on_s += dt
            if measured <= 0:
                # the whole hour was spent disconnected -- no row, so repair.py
                # sees a gap rather than a confident zero
                continue
            span = max(1.0, min(3600, measured))
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
        "MAX(w_max) w_max, SUM(CASE WHEN estimated THEN wh ELSE 0 END) est_wh, "
        "GROUP_CONCAT(DISTINCT method) methods "
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


def save_device(con, unit, device_id, activated_at, **fw):
    """Record identity and, when given, the firmware it reports running."""
    cols = ["unit", "device_id", "activated_at", "seen_at"]
    vals = [unit, device_id, activated_at, int(time.time())]
    for k in ("fw_version", "fw_build", "blynk_version", "board", "fw_updated_at"):
        if fw.get(k) is not None:
            cols.append(k); vals.append(fw[k])
    con.execute(f"INSERT OR REPLACE INTO devices({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(vals))})", vals)
    con.commit()


def firmware(con):
    """{unit: {version, build, blynk_version, board, updated_at}}."""
    return {r["unit"]: {"version": r["fw_version"], "build": r["fw_build"],
                        "blynk_version": r["blynk_version"], "board": r["board"],
                        "updated_at": r["fw_updated_at"]}
            for r in con.execute(
                "SELECT unit, fw_version, fw_build, blynk_version, board, "
                "fw_updated_at FROM devices")}


def activations(con):
    """{unit: activation unix ts}. Empty until device info has been fetched."""
    return {r["unit"]: r["activated_at"] for r in
            con.execute("SELECT unit, activated_at FROM devices WHERE activated_at")}


def covered_hours(con, unit, t0, t1):
    r = con.execute("SELECT COUNT(*) n FROM hourly WHERE unit=? AND hour>=? AND hour<?",
                    (unit, hour_of(t0), t1)).fetchone()
    return r["n"] if r else 0


def log_estimate(con, key, value, day=None):
    """Record today's value of a rolling estimate. First write of the day wins,
    so a page refresh cannot overwrite the day's figure with a mid-day one."""
    day = day or time.strftime("%Y-%m-%d")
    con.execute("INSERT OR IGNORE INTO estimate_log(day,key,value) VALUES (?,?,?)",
                (day, key, value))
    con.commit()


def estimate_history(con, key, limit=30):
    return [(r["day"], r["value"]) for r in con.execute(
        "SELECT day, value FROM estimate_log WHERE key=? ORDER BY day DESC LIMIT ?",
        (key, limit))][::-1]


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)", (k, str(v)))
    con.commit()


def get_meta(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default
