#!/usr/bin/env python3
"""Serve the Windmill AC dashboard and keep the local history growing.

    python3 serve.py            # http://localhost:8787

One process does three jobs: samples every unit on an interval into SQLite,
keeps the hourly rollup current, and serves the page. Leave it running --
Windmill only retains 30 days, so anything longer only exists here.
"""
import concurrent.futures
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import analysis, backfill, backup, blynk, calibrate, config, energy, filters, forecast, guard, identify, meter, notify, overrun, repair, schedule, store


PORT = config.PORT
BIND = config.HOST_BIND
SAMPLE_INTERVAL = 30
RELOAD = config.RELOAD

#: How the sync watchdog behaves. A unit is judged by whether the values it
#: *pushes* are still moving (see blynk.REPORTED) -- setpoints echo back from
#: the cloud regardless, so they prove nothing.
WATCH_INTERVAL = 120        # how often to judge every unit's sync health
WATCH_TICK = 10             # loop granularity; must divide the intervals above
#: Connected but no heartbeat for this long. Set well clear of the worst gap
#: seen on a healthy unit (~25 min) so an idle AC is never power-cycled for
#: being quiet -- the cost of a false positive is real hardware bouncing.
WEDGED_S = 3600
RESYNC_COOLDOWN = 3600      # never auto-resync the same unit more often
RESYNC_MAX = 3              # consecutive auto-resyncs before giving up on a unit

#: Cooling-with-no-demand is judged on an hour of history, so checking it every
#: couple of minutes would only re-read the same window. Its attempt limit is
#: lower than the sync one because the verdict is an inference rather than a
#: reported fault: if two cycles have not stopped a unit overcooling, the
#: problem is not one a resync fixes.
#:
#: The cooldown can be short because `overrun.report` judges a resynced unit
#: only on what it has done since -- so a retry is never triggered by the
#: misbehaviour that prompted the previous one. What actually paces retries is
#: overrun.MIN_RUN_S: a unit must accumulate real running time after a fix
#: before it can be judged at all.
#: The overrun verdict costs ~1 ms to compute from the database, so this is
#: set to act as soon as the page could show the problem rather than to save
#: work. The cooldown and attempt limit are what stop it acting repeatedly.
OVERRUN_INTERVAL = 30

#: The thermostat guard decides from a handful of DB reads, so it runs as
#: often as the page could show the problem. Its own MIN_OFF_S/MIN_ON_S are
#: what protect the compressor, not this interval.
GUARD_INTERVAL = 30
OVERRUN_COOLDOWN_DEFAULT = 1800
OVERRUN_MAX = 2

#: Bounds for the user-set cooldown. The floor is MIN_RUN_S plus a little, since
#: below that no unit could have run long enough post-fix to be judged and the
#: setting would do nothing but churn.
COOLDOWN_MIN, COOLDOWN_MAX = 600, 21600

#: A unit reports a transient state -- off, Eco, fan Low -- for minutes after a
#: software power cycle, while still connected. Observations are ignored for
#: this long after a resync starts, measured from the ~4 minutes these units
#: were seen taking to settle.
SETTLE_QUIET = 360

#: Most log rows to ship at once. The count of matches is sent alongside,
#: so a truncated view says so instead of looking complete.
EVENT_LIMIT = 300


#: Colour only when stdout is a terminal. Piped to a file or read by launchd,
#: escape codes are noise, so they are left out rather than written and ignored.
_TTY = sys.stdout.isatty()
_C = {"dim": "\033[38;5;245m", "bold": "\033[1m", "off": "\033[0m",
      "INFO": "\033[38;5;71m", "WARN": "\033[38;5;208m",
      "ERR": "\033[38;5;167m", "you": "\033[38;5;75m",
      "watchdog": "\033[38;5;140m", "alert": "\033[38;5;214m",
      "schedule": "\033[38;5;79m",
      "guard": "\033[38;5;169m"}


def _c(text, key):
    return f"{_C[key]}{text}{_C['off']}" if _TTY and key in _C else str(text)


def log(level, source, target, message, detail=None):
    """One line, one shape, for everything this process reports.

        18:03:48  INFO  you       Bedroom             floor -> 2

    Columns are fixed so a day of output stays scannable: time, level, who,
    what it happened to, then what happened. Detail is appended dimmed rather
    than given a column of its own, because it varies from empty to a sentence.
    """
    stamp = time.strftime("%H:%M:%S")
    line = (f"{_c(stamp, 'dim')}  {_c(f'{level:<4}', level)}  "
            f"{_c(f'{source:<8}', source if source in _C else 'dim')}  "
            f"{(target or '—'):<19} {message}")
    if detail:
        line += _c(f"  {detail}", "dim")
    print(line, flush=True)


_ANSI = re.compile(r"\033\[[0-9;]*m")


class _Tee:
    """stdout that also lands in a dated file.

    Wrapping the stream rather than adding a logging call at every print means
    everything is captured -- the banner, the action log, a traceback from a
    thread nobody thought about -- without any of it having to know. That is the
    whole point: the lines you most want in a file are the ones written by code
    that was not expecting to fail.

    Colour is stripped on the way to disk. Escape codes are for a terminal; in a
    file they are noise that breaks grep.
    """

    def __init__(self, stream, directory, keep):
        self.stream = stream
        self.dir = pathlib.Path(directory)
        self.keep = keep
        self.day = None
        self.fh = None
        self._open()

    def _open(self):
        day = time.strftime("%Y-%m-%d")
        if day == self.day and self.fh:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if self.fh:
                self.fh.close()
            self.fh = (self.dir / f"windmill-{day}.log").open("a", encoding="utf-8")
            self.day = day
            self._rotate()
        except Exception:
            # Never let logging break the thing being logged.
            self.fh = None

    def _rotate(self):
        """Keep the newest `keep` days. Matches only our own filenames, so
        nothing else in the directory can be caught by it."""
        try:
            ours = sorted(p for p in self.dir.glob("windmill-????-??-??.log")
                          if p.is_file())
            for old in ours[:-self.keep] if self.keep > 0 else []:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    def write(self, text):
        self.stream.write(text)
        if self.fh:
            try:
                if text.strip():
                    self._open()           # cheap; rolls over at midnight
                self.fh.write(_ANSI.sub("", text))
            except Exception:
                self.fh = None
        return len(text)

    def flush(self):
        self.stream.flush()
        if self.fh:
            try:
                self.fh.flush()
            except Exception:
                pass

    def isatty(self):
        return self.stream.isatty()

    def fileno(self):
        return self.stream.fileno()


def _who_has_port(port):
    """A one-line description of whatever is holding the port, if it can be
    found. Best effort -- this runs only on the way to exiting."""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) > 1:
                pid = parts[1]
                cmd = subprocess.run(["ps", "-o", "command=", "-p", pid],
                                     capture_output=True, text=True,
                                     timeout=5).stdout.strip()
                # The interpreter path is the least informative part of the
                # line; what matters is which script it is running.
                short = " ".join(
                    (part.rsplit("/", 1)[-1] if "/" in part else part)
                    for part in cmd.split())
                return f"pid {pid}  {short[:90]}"
    except Exception:
        pass
    return None


def start_file_log():
    """Send everything printed from here on to a dated file as well."""
    if not config.LOG_ENABLED:
        return None
    tee = _Tee(sys.stdout, config.LOG_DIR, config.LOG_KEEP)
    if tee.fh is None:
        return None
    sys.stdout = tee
    sys.stderr = _Tee(sys.stderr, config.LOG_DIR, config.LOG_KEEP)
    return tee


def record_action(target, action, detail=None, ok=True, source="you"):
    """Report one action and keep it in the audit trail.

    Both outputs come from here so they can never disagree: what scrolls past
    in the terminal is exactly what the Audit tab will show later.
    """
    log("INFO" if ok else "WARN", source, target, action,
        detail if ok else (detail or "failed"))
    try:
        con = store.connect()
        try:
            store.log_audit(con, source, target, action, detail, ok)
        finally:
            con.close()
    except Exception as exc:
        log("ERR", "audit", target, "could not be written", str(exc))


def _ago(ts, now=None):
    if not ts:
        return "never"
    d = max(0, (now or time.time()) - ts)
    for size, name in ((86400, "d"), (3600, "h"), (60, "min")):
        if d >= size:
            return f"{d / size:.0f}{name} ago"
    return "just now"


def banner(con):
    """What this process is, what it is doing, and where everything lives.

    Printed once at startup so the state a session begins in is on the record
    -- which database, which units, which switches are on -- rather than
    something to go and look up when output starts scrolling past.
    """
    now = time.time()
    lo, hi = store.bounds(con)
    db = store.DB
    size = f"{db.stat().st_size / 1e6:.1f} MB" if db.exists() else "new"
    n_readings = con.execute("SELECT COUNT(*) n FROM readings").fetchone()["n"]
    names = [d["name"] for d in config.DEVICES]
    try:
        due = [u for u, f in filters.report(con).items() if f["verdict"] == "due"]
    except Exception:
        due = []

    def row(k, v):
        print(f"  {_c(f'{k:<11}', 'dim')} {v}")

    print()
    print(f"  {_c('Windmill AC dashboard', 'bold')}")
    row("listening", _c(f"http://{BIND}:{PORT}", "you")
        + ("" if BIND == "127.0.0.1" else
           _c("   reachable off this machine — there is no auth", "WARN")))
    row("upstream", config.HOST)
    row("database", f"{db} ({size}, {n_readings:,} readings)")
    row("data dir", str(config.DATA))
    row("history", (f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(lo))} → "
                    f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(hi))}"
                    f"  ({(hi - lo) / 86400:.1f} days, last sample {_ago(hi, now)})")
        if lo else "nothing logged yet")
    row("units", f"{len(names)} — " + ", ".join(names))
    row("rate", f"${energy.rate(con):.3f}/kWh")
    row("sampling", f"every {SAMPLE_INTERVAL}s")
    row("watchdog", f"sync every {WATCH_INTERVAL}s · overrun every "
                    f"{OVERRUN_INTERVAL}s · auto-resync "
                    f"{'on' if auto_resync_enabled(con) else 'off'} · auto-fix "
                    f"overrun {'on' if auto_overrun_enabled(con) else 'off'} "
                    f"(cooldown {overrun_cooldown(con) // 60} min)")
    row("filters", _c(f"{len(due)} due — " + ", ".join(due), "WARN") if due
                   else "all reporting fine")
    row("log", f"{config.LOG_DIR}/windmill-{time.strftime('%Y-%m-%d')}.log "
               f"(keeping {config.LOG_KEEP} days)"
        if config.LOG_ENABLED else _c("screen only", "dim"))
    row("reload", f"watching {len(list(config.PKG.glob('*.py')))} .py files"
                  if RELOAD else "off")
    try:
        b = backup.status(con)
        row("backups", (f"{b['count']} in {b['dir']}, newest {b['newest']}"
                        if b.get("count") else
                        _c("none yet — history here cannot be re-fetched", "WARN")))
    except Exception:
        pass
    try:
        on = guard.enabled_units(con)
        row("guard", (", ".join(on) if on else "no unit opted in")
            + ("" if guard.guarding_enabled(con) else _c("  (master switch off)", "dim")))
    except Exception:
        pass
    ch = notify.channels()
    row("alerts", ", ".join(ch) if ch else
        _c("no channel configured — problems are shown but not sent", "dim"))
    print()


def overrun_cooldown(con):
    try:
        v = int(store.get_meta(con, "overrun_cooldown", OVERRUN_COOLDOWN_DEFAULT))
    except (TypeError, ValueError):
        v = OVERRUN_COOLDOWN_DEFAULT
    return max(COOLDOWN_MIN, min(COOLDOWN_MAX, v))

_lock = threading.Lock()


def migrate_jsonl(con):
    """One-time import of the original samples.jsonl into SQLite."""
    legacy = config.ROOT / "samples.jsonl"
    if not legacy.exists() or store.get_meta(con, "jsonl_imported"):
        return 0
    n = 0
    for line in legacy.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        n += store.write_sample(con, row["ts"], row.get("units", {}))
    store.set_meta(con, "jsonl_imported", int(time.time()))
    return n


def seed_defaults(con):
    """Give every unit a row in the database if it has none.

    A resync returns a unit to whatever the database says it should be, so the
    database needs an answer for every unit from the start -- otherwise the
    first resync of a never-yet-seen unit has nothing to restore. Only fills
    gaps: a unit with settings on file is left exactly as it is, so this can
    never overwrite something observed or set deliberately.
    """
    seeded = []
    for device in config.DEVICES:
        name = device["name"]
        if store.get_settings(con, name):
            continue
        store.save_settings(con, name, dict(blynk.DEFAULTS), "default")
        seeded.append(name)
    return seeded


def remember_settings(con, ts, units):
    """Keep each healthy unit's settings on file as the state to restore to.

    Only units holding a session are recorded. A wedged or dropped unit's
    reported settings are not evidence of anything -- writing them down would
    let a failure overwrite the very state a resync is supposed to put back.
    Recording from the poll (rather than only on a dashboard change) is what
    picks up settings changed in the Windmill app itself.
    """
    for unit, pins in units.items():
        if not isinstance(pins, dict) or pins.get(blynk.LINK) != 1:
            continue
        # A unit coming back from a resync reports a transient state -- off,
        # Eco, fan Low -- for minutes before settling into its real one. It is
        # connected throughout, so the link check does not catch it. Recording
        # that transient makes it the state the *next* resync restores, and the
        # unit converges on being switched off. Observations during and just
        # after a resync are not evidence of what anyone wants.
        if store.settling_until(con, unit, ts):
            continue
        # A unit the guard is holding off reports power=0, and that is the
        # guard's doing rather than anyone's preference. Recording it would make
        # "off" the state a later resync restores -- the same trap the settling
        # window closes for resyncs.
        if guard.held(con, unit):
            continue
        wanted = {p: pins[p] for p in blynk.WRITABLE if pins.get(p) is not None}
        if wanted and wanted != store.get_settings(con, unit):
            store.save_settings(con, unit, wanted, "observed", ts)


def sampler():
    con = store.connect()
    while True:
        try:
            ts = int(time.time())
            units = blynk.read_all()
            with _lock:
                store.write_sample(con, ts, units)
                remember_settings(con, ts, units)
                store.rebuild_hours(con, [store.hour_of(ts)], config.POWER_PIN,
                                    energy.COMPRESSOR_W, energy.MAX_GAP)
        except Exception as exc:
            log("ERR", "sampler", None, "poll failed", str(exc))
        time.sleep(SAMPLE_INTERVAL)


def backer():
    """Snapshot the database on a schedule.

    Windmill retains 30 days; anything older exists only in this file, so this
    is the only thing standing between a year of history and a disk failure.
    Wakes hourly rather than daily so a restart never skips a day -- the run
    itself decides whether one is due.
    """
    while True:
        try:
            con = store.connect()
            try:
                r = backup.run(con)
                if r["status"] == "ok":
                    log("INFO", "backup", None, r["detail"],
                        f"removed {', '.join(r['removed'])}" if r["removed"] else None)
                elif r["status"] == "error":
                    log("ERR", "backup", None, "failed", r["detail"])
            finally:
                con.close()
        except Exception as exc:
            log("ERR", "backup", None, "failed", str(exc))
        time.sleep(3600)


def maintainer():
    """Refresh device metadata, and keep retrying backfill for units with gaps.

    A unit can be missing history simply because its export quota was exhausted
    when we asked. That clears on its own, so retry rather than leaving a hole
    (or, worse, inventing numbers to fill it).
    """
    while True:
        try:
            con = store.connect()
            for device in config.DEVICES:
                info = blynk.device_info(device)
                if info and info["activated_at"]:
                    store.save_device(
                        con, device["name"], info["device_id"],
                        info["activated_at"],
                        **{k: info.get(k) for k in
                           ("fw_version", "fw_build", "blynk_version",
                            "board", "fw_updated_at")})

            with _lock:
                for r in backfill.fetch_filter_history(con):
                    if r["status"] == "ok" and r["rows"]:
                        log("INFO", "backfill", r["unit"],
                            f"imported {r['rows']} rows of {r['stream']}")
                for r in repair.run(con):
                    if r["status"] != "complete" and (r["recovered"] or r["estimated"]):
                        log("INFO", "repair", r["unit"],
                            f"{r['recovered']}h recovered, "
                            f"{r['estimated']}h reconstructed",
                            f"{r['missing_after']}h still missing")
            con.close()
        except Exception as exc:
            log("ERR", "maintain", None, "failed", str(exc))
        time.sleep(3600)


def sync_state(con, device, now=None):
    """Judge one unit: ok | wedged | dropped, with why.

    The three cases are genuinely different and want different responses:
      ok      -- session held and values still moving
      wedged  -- session held but the unit has gone quiet; a resync can reach
                 it, which is the case worth automating
      dropped -- no session at all; nothing sent arrives, so only mains power
                 will fix it and the watchdog must not pretend otherwise
    """
    now = int(now or time.time())
    name = device["name"]
    # The sampler already asks every SAMPLE_INTERVAL and stores the answer, so
    # read that rather than opening a second stream of API calls at watchdog
    # cadence. Only fall back to asking directly if no recent sample exists.
    link = store.latest(con, name, blynk.LINK, not_before=now - 3 * SAMPLE_INTERVAL)
    if link is None:
        live = blynk.connected(device)
        link = None if live is None else float(live)
    if link is None:
        return {"state": "unknown", "detail": "Could not reach the cloud."}
    changed = store.last_change(con, name, blynk.HEARTBEAT, now=now)
    quiet = None if changed is None else now - changed
    if not link:
        return {"state": "dropped", "quiet_s": quiet,
                "detail": "No session with the cloud."}
    if quiet is not None and quiet > WEDGED_S:
        return {"state": "wedged", "quiet_s": quiet,
                "detail": f"Connected but nothing reported for {quiet // 60} min."}
    return {"state": "ok", "quiet_s": quiet, "detail": "Reporting normally."}


def do_resync(con, device, trigger=None):
    """Resync a unit back to its stored settings, recording what it was told.

    `trigger` labels a resync the caller is not otherwise logging -- the
    watchdog writes its own event carrying the verdict that prompted it, but a
    resync asked for from the page would otherwise leave only a snapshot row
    and no record that anything was done.
    """
    name = device["name"]
    desired = store.get_settings(con, name)
    # Cover the cycle and the minutes of transient reporting that follow it, so
    # nothing observed in that window is mistaken for a deliberate setting.
    store.mark_settling(con, name, time.time() + SETTLE_QUIET)
    result = blynk.resync(
        device, desired=desired,
        record=lambda s: store.log_sync(
            con, name, None, "snapshot", json.dumps(s, sort_keys=True)))
    # A successful restore is the state to keep; a failed one must not become
    # the new stored truth, or the next resync would put the unit back wrong.
    if result.get("ok") and result.get("restored"):
        store.save_settings(con, name, result["restored"], "restored")
    if trigger:
        store.log_sync(con, name, "requested", trigger,
                       result.get("detail") or result.get("reason") or "")
    return result


def devices_named(names):
    """Config entries for the given unit names, in configured order."""
    wanted = set(names)
    return [d for d in config.DEVICES if d["name"] in wanted]


def in_parallel(devices, fn):
    """Run fn(device) across devices at once, keeping results per unit.

    Each unit is independent, so one failing or timing out must not hold up or
    fail the rest -- a floor-wide change that half-applied silently would be
    worse than one that reports exactly which units took it.
    """
    if not devices:
        return {}
    with concurrent.futures.ThreadPoolExecutor(len(devices)) as ex:
        futures = {ex.submit(fn, d): d["name"] for d in devices}
        out = {}
        for f in concurrent.futures.as_completed(futures):
            name = futures[f]
            try:
                out[name] = f.result()
            except Exception as exc:
                out[name] = {"ok": False, "reason": "failed",
                             "detail": f"{type(exc).__name__}: {exc}"}
    return out


def bulk_summary(results, action=None):
    """Fold per-unit results into one answer, and name the units that failed.

    A bulk call logs one summary line, which is right for a page but wrong for
    an audit trail: "5 of 7 units" does not say which two. Any unit that failed
    also gets its own audit row, so the trail can still answer that later.
    """
    ok = [n for n, r in results.items() if r.get("ok")]
    bad = {n: r.get("detail") or r.get("reason") for n, r in results.items()
           if not r.get("ok")}
    if action:
        for name, why in sorted(bad.items()):
            record_action(name, action, why or "did not take", False)
    return {"ok": not bad, "applied": sorted(ok), "failed": bad,
            "results": results,
            "detail": (f"{len(ok)} of {len(results)} units"
                       + (f"; {', '.join(sorted(bad))} did not take" if bad else ""))}


def watchdog_status(con, device, over, now):
    """Everything the watchdog knows about one unit, for the Watchdog tab.

    `held_until` is why nothing is happening to a unit that looks flagged: the
    watchdog is deliberately waiting rather than cycling it again, and a page
    that shows a problem with no explanation for the inaction reads as broken.
    """
    name = device["name"]
    sync = sync_state(con, device, now=now)
    a = over.get(name, {})
    last_sync_fix = store.last_action(con, name, "auto-resync")
    last_over_fix = store.last_action(con, name, "overrun-resync")

    held, reason = None, None
    cool = overrun_cooldown(con)
    if last_over_fix and now - last_over_fix < cool:
        held, reason = last_over_fix + cool, "overrun cooldown"
    elif last_sync_fix and now - last_sync_fix < RESYNC_COOLDOWN:
        held, reason = last_sync_fix + RESYNC_COOLDOWN, "resync cooldown"

    attempts = store.count_actions(
        con, name, ("auto-resync", "overrun-resync"), now - 86400)
    gave_up = any(e["action"] == "skipped" for e in
                  store.sync_events(con, unit=name, since=now - 86400, limit=5))
    return {
        **sync,
        "sync": sync["state"],
        "overrun": a.get("verdict"),
        "overrun_detail": a.get("detail"),
        "duty": a.get("duty"),
        "floor_duty": a.get("floor_duty"),
        "floor": a.get("floor"),
        "peers": a.get("peers"),
        "saved": blynk.settings_label(store.get_settings(con, name)),
        "saved_at": store.settings_meta(con, name).get("updated_at"),
        # Undocumented, constant per unit, and the only thing found that
        # predicts which units misbehave. See renderRevisions in the page.
        "revision": store.latest(con, name, "v113"),
        "failures": store.latest(con, name, blynk.FAILURES),
        "last_resync": last_sync_fix,
        "last_overrun_resync": last_over_fix,
        "held_until": held,
        "held_reason": reason,
        "attempts_24h": attempts,
        "gave_up": gave_up,
    }


def auto_resync_enabled(con):
    return store.get_meta(con, "auto_resync", "1") == "1"


def auto_overrun_enabled(con):
    """Separate switch from auto-resync: overcooling is inferred, not reported,
    so it is reasonable to want the certain fix automated and this one not."""
    return store.get_meta(con, "auto_overrun", "1") == "1"


def check_overrun(con):
    """Resync units that are cooling when nothing asked them to."""
    report = overrun.report(con, int(time.time()))
    for name, a in report.items():
        device = next((d for d in config.DEVICES if d["name"] == name), None)
        if not device:
            continue
        if a["verdict"] == "ok":
            # Clearing the flag resets the attempt counter, so a unit that
            # relapses later gets a fresh set of tries rather than being written
            # off forever by strikes it earned days ago.
            store.note_recovery(con, name, a["detail"])
            continue

        last = store.last_action(con, name, "overrun-resync")
        if last and time.time() - last < overrun_cooldown(con):
            continue                       # already tried recently; let it settle
        tries = store.attempts_since_recovery(con, name, ("overrun-resync",))
        if tries >= OVERRUN_MAX:
            store.log_sync(con, name, a["verdict"], "skipped",
                           f"still overrunning after {OVERRUN_MAX} resyncs -- "
                           f"not something a resync fixes. {a['detail']}")
            if tries == OVERRUN_MAX:      # say it once, not on every check
                record_action(name, "gave up on overrunning",
                              f"still overrunning after {OVERRUN_MAX} resyncs; "
                              f"not something a resync fixes", False,
                              source="watchdog")
            continue
        if not auto_overrun_enabled(con):
            store.log_sync(con, name, a["verdict"], None,
                           a["detail"] + " Auto-resync for overrunning is off.")
            continue
        # A unit with no session cannot be told anything, overrunning or not.
        if sync_state(con, device)["state"] == "dropped":
            store.log_sync(con, name, a["verdict"], None,
                           a["detail"] + " Cannot reach it to do anything about it.")
            continue

        result = do_resync(con, device)
        store.log_sync(con, name, a["verdict"], "overrun-resync",
                       f"attempt {tries + 1}: {a['detail']} "
                       f"-> {'ok' if result.get('ok') else result.get('reason')}")
        record_action(name, f"resync ({a['verdict']})",
                      result.get("detail") or result.get("reason"),
                      bool(result.get("ok")), source="watchdog")


def watchdog():
    """Watch every unit's sync health and resync the ones that can be fixed.

    Deliberately conservative: it only acts on `wedged`, respects a cooldown,
    and stops after RESYNC_MAX consecutive attempts rather than power-cycling a
    unit forever. Everything it sees and does is written to sync_events.
    """
    con = store.connect()
    next_overrun = next_sync = next_guard = 0.0
    while True:
        try:
            now = time.time()
            # Two checks on two schedules, so the loop ticks faster than either
            # of them. Sleeping for the sync interval used to cap how often the
            # overrun check could run, no matter what its own interval said.
            run_schedules(con)
            check_restore(con)
            if now >= next_guard:
                next_guard = now + GUARD_INTERVAL
                guard.run(con, blynk.control,
                          record=lambda d, r: record_action(
                              d["unit"], f"guard: {d['action']}",
                              r.get("detail") or d.get("detail"),
                              r.get("ok", False), source="guard"))
            if now >= next_overrun:
                next_overrun = now + OVERRUN_INTERVAL
                check_overrun(con)
            if now >= next_sync:
                next_sync = now + WATCH_INTERVAL
                check_sync(con)
                check_rejections(con)
                check_filters(con)
                check_alerts(
                    con,
                    {d["name"]: sync_state(con, d) for d in config.DEVICES},
                    filters.report(con))
        except Exception as exc:
            log("ERR", "watchdog", None, "check failed", str(exc))
        time.sleep(WATCH_TICK)


def check_filters(con):
    """Announce a filter that has newly started asking to be changed.

    Only on the transition. A filter stays dirty until someone physically
    changes it, so repeating the notice every couple of minutes would turn the
    audit trail into noise and train you to ignore it.
    """
    for unit, f in filters.report(con).items():
        was_due = store.get_meta(con, f"filter_due:{unit}", "0") == "1"
        now_due = f["verdict"] == "due"
        if now_due == was_due:
            continue
        store.set_meta(con, f"filter_due:{unit}", "1" if now_due else "0")
        if now_due:
            record_action(unit, "filter needs changing", f["detail"], False,
                          source="watchdog")
        else:
            record_action(unit, "filter changed",
                          "The unit stopped asking; runtime counter reset.",
                          True, source="watchdog")


def run_schedules(con):
    """Apply any rule whose moment has just passed."""
    for rule in schedule.due(con):
        def record(r, results, ok, bad, _rule=rule):
            record_action(
                schedule.describe(_rule, con).split(" — ")[1],
                "scheduled change",
                f"{len(ok)} of {len(results)} units"
                + (f"; {', '.join(sorted(bad))} did not take" if bad else ""),
                not bad, source="schedule")
        schedule.apply(con, rule, blynk.control, record=record)


def check_alerts(con, sync_states, filter_report):
    """Announce the problems a person has to deal with."""
    now = time.time()
    gave_up = {}
    for e in store.sync_events(con, since=now - 86400, limit=200):
        if e["action"] == "skipped" and "still overrunning" in (e["detail"] or ""):
            gave_up[e["unit"]] = e["detail"]
    items = notify.pending(con, sync_states, filter_report, gave_up, now)
    # A problem that has cleared should alert again if it returns.
    live = {(k, u) for k, u, _, _ in items}
    for kind in notify.KINDS:
        for device in config.DEVICES:
            if (kind, device["name"]) not in live:
                notify.clear(con, kind, device["name"])
    return notify.dispatch(con, items, now, record=lambda *a: record_action(
        a[0], a[1], a[2], a[3], source="alert"))


#: How many times to re-assert settings a unit is drifting away from before
#: giving up on its stored state and falling back to the configured defaults.
RESTORE_TRIES = 2


def check_restore(con):
    """Put a unit back where it should be after a resync, and keep it there.

    A resync writes the stored settings and the cloud accepts them, but some of
    these units do not hold them -- Bedroom drifts back to Eco/Low within
    minutes and can switch itself off entirely. Writing once and walking away
    means "resync" reads as "turns my unit off", which is exactly how it feels
    from the outside.

    So for as long as a unit is settling, its reported state is compared with
    what it was told, and any difference is written again. Only the settle
    window is policed, because that is the one period where a difference is
    attributable to our own write rather than to a person at the app -- outside
    it, an observed change *is* the new intent and `remember_settings` records
    it as such.

    After RESTORE_TRIES the stored state is treated as the thing at fault and
    the configured defaults are applied instead, on the grounds that a unit
    running on Cool/Auto is better than one that keeps switching itself off.
    """
    now = int(time.time())
    for device in config.DEVICES:
        unit = device["name"]
        if not store.settling_until(con, unit, now):
            continue
        if guard.held(con, unit):
            continue                       # off on purpose; not drift
        wanted = store.get_settings(con, unit)
        if not wanted:
            continue

        fresh = now - 3 * SAMPLE_INTERVAL
        actual = {p: store.latest(con, unit, p, not_before=fresh)
                  for p in blynk.WRITABLE}
        if any(v is None for v in actual.values()):
            continue                       # nothing recent enough to judge
        wrong = {p: v for p, v in wanted.items()
                 if p in actual and blynk.num(actual[p]) != blynk.num(v)}
        key = f"restore_tries:{unit}"
        if not wrong:
            store.set_meta(con, key, 0)
            continue

        try:
            tries = int(store.get_meta(con, key, 0) or 0)
        except (TypeError, ValueError):
            tries = 0
        if tries >= RESTORE_TRIES:
            # Its stored state keeps being refused. Stop insisting on it and put
            # the unit somewhere sane instead.
            if blynk.num(actual.get(blynk.POWER)) == 1 and \
               all(blynk.num(actual[p]) == blynk.num(v)
                   for p, v in blynk.DEFAULTS.items() if p in actual):
                continue                   # already on the defaults; leave it
            r = blynk.apply_settings(device, blynk.DEFAULTS, verify=False, settle=0)
            fixed = r.get("written") or []
            store.save_settings(con, unit, dict(blynk.DEFAULTS), "default")
            store.set_meta(con, key, 0)
            record_action(unit, "reset to defaults",
                          f"it would not hold its saved settings after "
                          f"{RESTORE_TRIES} attempts, so it is now "
                          f"{blynk.settings_label(blynk.DEFAULTS)}"
                          + (f" ({', '.join(fixed)} rewritten)" if fixed else ""),
                          bool(fixed), source="watchdog")
            continue

        store.set_meta(con, key, tries + 1)
        # Same writer as the resync and the guard use. Not verifying here: this
        # pass IS the verification, and it runs again on the next tick.
        r = blynk.apply_settings(device, wrong, power_last=blynk.POWER in wrong,
                                 verify=False, settle=0)
        record_action(unit, "drifted after a resync",
                      f"re-sent {', '.join(r['written']) or 'nothing'} "
                      f"(attempt {tries + 1} of {RESTORE_TRIES}); it had moved to "
                      f"{blynk.settings_label(actual)}",
                      bool(r.get("written")), source="watchdog")


def check_rejections(con):
    """Notice a unit that switches itself off just after we wrote to it.

    Bedroom does this: it drifts back to Eco/Low on its own, and a write of
    Cool/Auto is followed within seconds by the unit powering off. That is the
    hardware refusing the settings, not a failed write -- the cloud accepted
    every one of them. Left undetected it looks like "resync turns the unit
    off", which is what it feels like from the outside.

    Only the settle window is examined, because that is exactly the period when
    a state change is attributable to our own write rather than to a person.
    """
    now = int(time.time())
    for device in config.DEVICES:
        unit = device["name"]
        until = store.settling_until(con, unit, now)
        if not until:
            continue
        wanted = store.get_settings(con, unit)
        if blynk.num(wanted.get(blynk.POWER)) != 1:
            continue                       # it was meant to be off anyway
        power = store.latest(con, unit, blynk.POWER,
                             not_before=now - 2 * SAMPLE_INTERVAL)
        if power is None or power == 1:
            continue
        key = f"rejected:{unit}"
        if store.get_meta(con, key, "0") == str(int(until)):
            continue                       # already said so for this window
        store.set_meta(con, key, int(until))
        record_action(unit, "switched itself off after a write",
                      "The settings were accepted by the cloud and the unit "
                      "powered off anyway -- it is refusing them, not failing "
                      "to receive them.", False, source="watchdog")


def check_sync(con):
    """Resync units that hold a session but have stopped reporting."""
    for device in config.DEVICES:
        name = device["name"]
        st = sync_state(con, device)
        if st["state"] == "ok":
            store.note_recovery(con, name, st["detail"])
            continue

        last = store.last_action(con, name, "auto-resync")
        cooling_off = last and time.time() - last < RESYNC_COOLDOWN
        if st["state"] != "wedged":
            # dropped/unknown: record it, but do not pretend a write helps
            store.log_sync(con, name, st["state"], None, st["detail"])
            continue
        tries = store.attempts_since_recovery(con, name, ("auto-resync",))
        if cooling_off or tries >= RESYNC_MAX:
            store.log_sync(con, name, st["state"], "skipped",
                           "cooling off" if cooling_off else
                           f"gave up after {RESYNC_MAX} attempts")
            continue

        if not auto_resync_enabled(con):
            store.log_sync(con, name, st["state"], None,
                           st["detail"] + " Auto-resync is off.")
            continue

        result = do_resync(con, device)
        store.log_sync(con, name, st["state"], "auto-resync",
                       f"attempt {tries + 1}: "
                       f"{'ok' if result.get('ok') else result.get('reason')}")
        record_action(name, f"resync (not syncing, attempt {tries + 1})",
                      result.get("detail") or result.get("reason"),
                      bool(result.get("ok")), source="watchdog")


#: The API, described as data so the page documenting it cannot drift from the
#: routes below. `effect` drives whether the docs tab will let you run a call:
#: reads are runnable, anything that moves a setting or an AC is not.
RANGE_PARAMS = [
    {"name": "range", "does": "Named preset. Ignored if from/to are given."},
    {"name": "from", "does": "Start, unix seconds. Needs `to`; overrides `range`."},
    {"name": "to", "does": "End, unix seconds."},
    {"name": "label", "does": "Display name for a custom range."},
]

API_DOCS = [
    {"path": "/api/summary", "group": "History", "effect": "read",
     "summary": "Everything the dashboard renders for a time range.",
     "params": RANGE_PARAMS,
     "returns": "units[] with live state, energy, sync and overrun verdicts; "
                "totals; chart series; indoor/outdoor temps; coverage."},
    {"path": "/api/unit", "group": "History", "effect": "read",
     "summary": "One unit's full series, for its own chart.",
     "params": [{"name": "unit", "required": True, "does": "Unit name."}] + RANGE_PARAMS,
     "returns": "series.{watts,temp,target,power,rssi,link} as [ts,value] pairs, "
                "series.failures as [ts,increment,total], and totals. "
                "range.trimmed is true when history starts later than asked."},
    {"path": "/api/comparison", "group": "History", "effect": "read",
     "summary": "Weather-normalised before/after around a change date.",
     "params": [{"name": "change", "does": "YYYY-MM-DD; defaults to config."},
                {"name": "new", "does": "Comma-separated unit names."}],
     "returns": "available, plus per-period energy normalised by degree-days."},

    {"path": "/api/identify", "group": "Maintenance", "effect": "settings",
     "summary": "Find which virtual pin a named feature uses, by watching which "
                "one moves while you toggle it in the Windmill app.",
     "params": [{"name": "action", "values": "start | confirm | stop",
                 "does": "Omit to read the current watch."},
                {"name": "unit", "does": "Which unit to watch."},
                {"name": "label", "does": "What you are about to toggle."},
                {"name": "pin", "does": "The pin to bind, for confirm."}],
     "returns": "the candidate pins being watched, which have moved, and a "
                "verdict of waiting | found | ambiguous. Read-only against the "
                "hardware -- it never writes to an unidentified pin."},
    {"path": "/api/schedules", "group": "Control", "effect": "settings",
     "summary": "Read, add, remove, enable or run time-of-day rules.",
     "params": [{"name": "action", "values": "add | edit | delete | toggle | run",
                 "does": "Omit to read."},
                {"name": "scope",
                 "does": "Unit name, floor:<name>, or empty for everything."},
                {"name": "field", "does": "power | target | mode | fan."},
                {"name": "value", "does": "Validated the same as /api/control."},
                {"name": "at", "does": "Minutes past local midnight."},
                {"name": "days", "does": "Digits 0=Mon..6=Sun, e.g. 01234."},
                {"name": "id", "does": "Which rule, for edit/delete/toggle/run."},
                {"name": "on", "values": "0 | 1", "does": "For toggle."}],
     "returns": "rules with a plain-English `text`, the next few firings, and "
                "the scopes and fields a rule may use."},
    {"path": "/api/filter-history", "group": "Maintenance", "effect": "settings",
     "summary": "Import filter indicator and runtime history from Windmill's "
                "export API, to find past filter changes and learn the runtime "
                "at which a filter is called dirty.",
     "params": [{"name": "force", "values": "1",
                 "does": "Re-fetch even if already cached."}],
     "returns": "per-unit import results and the learned threshold. Two "
                "exports per unit against the 72/day quota; the hourly "
                "maintainer retries until it lands."},
    {"path": "/api/backup", "group": "Maintenance", "effect": "settings",
     "summary": "Snapshot the database, or report on existing snapshots.",
     "params": [{"name": "run", "values": "1",
                 "does": "Take one now. Omit to only report."}],
     "returns": "with run: path, bytes, duration, what was rotated out, and the "
                "verification result. Otherwise: how many snapshots exist, "
                "newest and oldest, and whether one is due."},
    {"path": "/api/guard", "group": "Control", "effect": "settings",
     "summary": "The software thermostat guard: what it makes of each unit, and "
                "which units it is allowed to switch off.",
     "params": [{"name": "auto", "values": "0 | 1",
                 "does": "Master switch. Omit to only read."},
                {"name": "unit", "does": "Which unit to opt in or out."},
                {"name": "on", "values": "0 | 1", "does": "With `unit`."}],
     "returns": "per-unit decision with a stable `reason` and a sentence saying "
                "why it did or did not act, plus the thresholds in force. Opt-in "
                "per unit; it never touches a unit that was not enabled."},
    {"path": "/api/forecast", "group": "History", "effect": "read",
     "summary": "Projected cost of the current month, weather-normalised.",
     "params": [],
     "returns": "measured month-to-date, a projected remainder with a low-high "
                "band, per-unit breakdown, the fit it rests on, and caveats[] "
                "in plain sentences. Refuses with a reason when the month is "
                "too young to project rather than guessing."},
    {"path": "/api/calibrate", "group": "History", "effect": "read",
     "summary": "Grade candidate integration rules against the units' own "
                "energy counters, and say which one the evidence prefers.",
     "params": RANGE_PARAMS,
     "returns": "per-unit signed error for each candidate rule, per-rule MAPE "
                "and kWh-weighted MAPE, the same ranking with the heaviest unit "
                "excluded, an error-versus-poll-interval sweep, and a verdict. "
                "Reports only; nothing is applied. Takes about a second, so it "
                "is not part of the dashboard poll."},
    {"path": "/api/meter", "group": "History", "effect": "read",
     "summary": "Our integrated kWh against each unit's own energy counter.",
     "params": RANGE_PARAMS,
     "returns": "per-unit ours_kwh, meter_kwh, delta_pct, coverage and a "
                "verdict; plus a fleet factor, which is reported and not "
                "applied. Windows are narrowed to what the meter covers, so "
                "the two sides always describe the same span."},
    {"path": "/api/audit", "group": "Watchdog", "effect": "read",
     "summary": "Every action actually performed, by you or by the watchdog.",
     "params": [{"name": "range", "values": "a preset, or all",
                 "does": "Default 24h."},
                {"name": "source", "values": "you | watchdog",
                 "does": "Who performed it."},
                {"name": "target", "does": "Unit name."},
                {"name": "from", "does": "Custom range start, unix seconds."},
                {"name": "to", "does": "Custom range end."}],
     "returns": "events[] of {ts, source, target, action, detail, ok}, the "
                "available sources and targets, and range.total."},
    {"path": "/api/sync", "group": "Watchdog", "effect": "read",
     "summary": "What the watchdog sees, what it has done, and its switches.",
     "params": [{"name": "auto", "values": "0 | 1",
                 "does": "Set auto-resync. Omit to only read."},
                {"name": "overrun", "values": "0 | 1",
                 "does": "Set auto-fix overrun. Omit to only read."},
                {"name": "cooldown",
                 "does": "Seconds between overrun fixes, clamped to 10 min - 6 h."},
                {"name": "range", "values": "a preset, or all",
                 "does": "Which slice of the event log to return. Default 24h."},
                {"name": "from", "does": "Start of a custom log range, unix seconds."},
                {"name": "to", "does": "End of a custom log range."}],
     "returns": "auto, overrun_auto, config thresholds, per-unit verdicts with "
                "cooldown state, and the event log for the range, with "
                "events_range.total saying how many matched."},

    {"path": "/api/control", "group": "Control", "effect": "hardware",
     "summary": "Change one setting on one unit.",
     "params": [{"name": "unit", "required": True, "does": "Unit name."},
                {"name": "field", "required": True, "does": "See control fields below."},
                {"name": "value", "required": True, "does": "Validated and clamped."}],
     "returns": "ok, pin, value. 409 if the unit has no session; 400 on a bad value."},
    {"path": "/api/floor/control", "group": "Control", "effect": "hardware",
     "summary": "Change one setting across a whole floor, in parallel.",
     "params": [{"name": "floor", "does": "Floor name. Omit to hit every unit."},
                {"name": "field", "required": True, "does": "See control fields below."},
                {"name": "value", "required": True, "does": "Validated once before fanning out."}],
     "returns": "ok, applied[], failed{}, and per-unit results."},
    {"path": "/api/resync", "group": "Control", "effect": "hardware",
     "summary": "Power-cycle one unit in software and restore its saved settings.",
     "params": [{"name": "unit", "required": True, "does": "Unit name."}],
     "returns": "ok, preserved (what it was restored to), missed[], failure counts."},
    {"path": "/api/resync-all", "group": "Control", "effect": "hardware",
     "summary": "Resync a floor, or every unit.",
     "params": [{"name": "floor", "does": "Floor name. Omit for all units."}],
     "returns": "ok, applied[], failed{}."},

    {"path": "/api/units", "group": "Setup", "effect": "settings",
     "summary": "The AC units themselves: list, add, rename, remove.",
     "params": [{"name": "action", "values": "add | label | rename | remove | toggle",
                 "does": "Omit to list."},
                {"name": "name", "does": "Unit name."},
                {"name": "token", "does": "Its Blynk device token, for add."},
                {"name": "to", "does": "New name, for rename."},
                {"name": "label", "does": "Display name, for label. Changing this moves nothing; renaming moves every row."},
                {"name": "purge", "values": "1",
                 "does": "With remove: delete its history too. Off by default."},
                {"name": "on", "values": "0 | 1", "does": "For toggle."}],
     "returns": "each unit with its firmware version, build and last update, the last four characters of its token and nothing more -- the token itself is never returned. A rename "
                "carries every row keyed by the old name with it."},
    {"path": "/api/floors", "group": "Setup", "effect": "settings",
     "summary": "Read floor assignments, or assign one.",
     "params": [{"name": "unit", "does": "Unit to assign. Omit to only read."},
                {"name": "floor", "does": "Floor name. Empty string clears it."}],
     "returns": "floors{unit: floor} and the sorted list of floor names."},
    {"path": "/api/rate", "group": "Setup", "effect": "settings",
     "summary": "Read or set the electricity rate, applied to every range.",
     "params": [{"name": "set", "does": "New $/kWh. Omit to only read."}],
     "returns": "rate."},

    {"path": "/api/repair", "group": "Maintenance", "effect": "settings",
     "summary": "Recover missing history, then reconstruct hours a unit ran "
                "without reporting.",
     "params": [{"name": "dry", "values": "1", "does": "Report only; change nothing."}],
     "returns": "per-unit recovered/estimated/still-missing hours."},
    {"path": "/api/backfill", "group": "Maintenance", "effect": "settings",
     "summary": "Pull history from Windmill's export API. Subject to its "
                "72-exports-per-day quota.",
     "params": [{"name": "force", "values": "1", "does": "Re-fetch ranges already cached."}],
     "returns": "per-unit fetch results."},
]


def api_reference():
    """Docs plus the live reference data a caller needs, read from the modules
    that define it rather than restated here."""
    return {
        "endpoints": API_DOCS,
        "upstream": blynk.UPSTREAM,
        "upstream_host": config.HOST,
        "dsids": [{"id": i, "name": n, "pin": p}
                  for i, (n, p) in sorted(blynk.DSID_MAP.items())],
        "ranges": list(energy.PRESET_NAMES),
        "controls": {name: {
            "pin": spec["pin"],
            "accepts": (f"{spec['clamp'][0]}-{spec['clamp'][1]}" if "clamp" in spec
                        else ", ".join(str(v) for v in spec["choices"])),
            "meaning": ({"power": "0 off, 1 on",
                         "target": "degrees F, clamped to this range",
                         "mode": ", ".join(f"{k} {v.lower()}"
                                           for k, v in blynk.MODES.items()),
                         "fan": ", ".join(f"{k} {v.lower()}"
                                          for k, v in blynk.FANS.items())})[name],
        } for name, spec in blynk.CONTROLS.items()},
        "units": [d["name"] for d in config.DEVICES],
        "notes": [
            "Every route is a GET, including the ones that change things -- this "
            "serves one local page and adding verbs would not make it safer.",
            "Responses are always JSON. Anything else means the route does not "
            "exist on the running server and the request fell through to the page.",
            f"The synthetic pin {blynk.LINK!r} is this tool's own record of whether "
            "the unit held a cloud session; it is not a Windmill datastream.",
        ],
    }


def reloader(interval=1.0):
    """Restart the process when any .py file in the project changes.

    `index.html` is deliberately not watched: it is read from disk on every
    request, so page edits are already live and restarting for them would only
    interrupt sampling for no reason.

    A changed file is compiled before restarting. Half-written files and syntax
    errors are common while editing, and exec-ing into one would take the
    sampler down until it was noticed -- better to stay up on the last good
    version and say what is wrong.
    """
    def stamps():
        return {p: p.stat().st_mtime for p in config.PKG.glob("*.py")
                if p.exists()}

    known = stamps()
    broken = None
    while True:
        time.sleep(interval)
        try:
            now = stamps()
        except OSError:
            continue
        changed = [p for p, m in now.items() if known.get(p) != m]
        if not changed:
            continue
        known = now
        try:
            for p in changed:
                compile(p.read_text(), str(p), "exec")
        except (SyntaxError, ValueError) as exc:
            if broken != str(exc):
                broken = str(exc)
                log("WARN", "reload", getattr(exc, "filename", "?"),
                    "held — file will not compile",
                    f"line {getattr(exc, 'lineno', '?')}: "
                    f"{getattr(exc, 'msg', exc)}")
            continue
        broken = None
        names = ", ".join(sorted(p.name for p in changed))
        log("INFO", "reload", names, "changed — restarting")
        sys.stdout.flush()
        # Same PID, so anything supervising this process is undisturbed.
        os.execv(sys.executable, [sys.executable, *sys.argv])


#: Paths that only ever read. The page polls these every 30 seconds, so logging
#: them would bury the things you actually did under its own background noise.
READ_ONLY = {"/api/summary", "/api/unit", "/api/comparison", "/api/docs",
             "/api/audit", "/api/meter", "/api/calibrate",
             "/api/forecast"}

#: Human-readable modes for a couple of the raw control values.
_MODE_NAME = {0: "Fan", 1: "Cool", 2: "Eco"}
_FAN_NAME = {0: "Auto", 1: "Low", 2: "Medium", 3: "High"}


def describe_action(path, q, obj, code):
    """(target, action, detail) for something that changed, or None for a read.

    Written from the request and its response together, so the line says what
    was asked for *and* what came of it -- "set target 71" alone would read as
    success even when the unit refused it.
    """
    one = lambda k, d=None: (q.get(k) or [d])[0]
    ok = 200 <= code < 300 and obj.get("ok", True)

    if path == "/api/control":
        # Report the value that was applied, not the one asked for -- setpoints
        # are clamped, and logging the request would claim something untrue.
        applied = obj.get("value")
        asked = one("value")
        shown = str(applied) if applied is not None else asked
        note = (f"asked for {asked}, clamped"
                if applied is not None and str(applied) != str(_int(asked))
                else None)
        return (one("unit"), _setting_phrase(one("field"), shown),
                obj.get("detail") if not ok else note)

    if path == "/api/floor/control":
        where = f"floor {one('floor')}" if one("floor") else "all units"
        return (where, _setting_phrase(one("field"), one("value")),
                obj.get("detail"))

    if path == "/api/resync":
        return (one("unit"), "resync",
                obj.get("detail") or obj.get("reason"))

    if path == "/api/resync-all":
        return (f"floor {one('floor')}" if one("floor") else "all units",
                "resync", obj.get("detail"))

    if path == "/api/units" and one("action"):
        return (one("name"), f"{one('action')} unit"
                + (f" -> {one('to')}" if one("action") == "rename" else ""),
                obj.get("detail"))

    if path == "/api/floors" and one("unit"):
        f = one("floor", "")
        return (one("unit"), f"floor -> {f}" if f else "floor cleared", None)

    if path == "/api/rate" and one("set"):
        return (None, f"rate -> ${obj.get('rate')}/kWh", None)

    if path == "/api/sync":
        bits = []
        if one("auto") in ("0", "1"):
            bits.append(f"auto-resync {'on' if one('auto') == '1' else 'off'}")
        if one("overrun") in ("0", "1"):
            bits.append(f"auto-fix overrun {'on' if one('overrun') == '1' else 'off'}")
        if one("cooldown"):
            bits.append(f"overrun cooldown -> "
                        f"{obj.get('config', {}).get('overrun_cooldown_s', 0) // 60} min")
        return (None, ", ".join(bits), None) if bits else None

    if path == "/api/repair":
        return (None, "repair gaps" + (" (dry run)" if one("dry") == "1" else ""),
                _count_detail(obj.get("results")))
    if path == "/api/backfill":
        return (None, "backfill history" + (" (forced)" if one("force") == "1" else ""),
                _count_detail(obj.get("results")))
    return None


def _setting_phrase(field, value):
    """'set mode Eco' rather than 'set mode 2' -- the raw codes are only
    meaningful next to a table nobody has open while reading a log."""
    pretty = {
        "target": lambda v: f"{v}°F",
        "mode": lambda v: _MODE_NAME.get(_int(v), v),
        "fan": lambda v: _FAN_NAME.get(_int(v), v),
        "power": lambda v: "on" if v == "1" else "off",
    }.get(field, lambda v: v)
    return f"set {field} {pretty(value)}"


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _count_detail(results):
    if not isinstance(results, list):
        return None
    return f"{len(results)} unit{'' if len(results) == 1 else 's'}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _action_of(self, obj, code):
        """{target, action, detail, ok} for a request that changed something.

        Computed before the response goes out so the same description can ride
        along in it -- the browser console then narrates an action in exactly
        the words the terminal used, instead of parsing the URL a second time
        and slowly drifting from them.
        """
        try:
            url = urllib.parse.urlparse(self.path)
            if url.path in READ_ONLY or not url.path.startswith("/api/"):
                return None
            what = describe_action(url.path, urllib.parse.parse_qs(url.query),
                                   obj if isinstance(obj, dict) else {}, code)
            if not what:
                return None
            target, action, detail = what
            return {"target": target, "action": action, "detail": detail,
                    "ok": 200 <= code < 300 and (obj.get("ok", True)
                                                 if isinstance(obj, dict) else True)}
        except Exception:
            return None

    def _send(self, body, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        act = self._action_of(obj, code)
        if act and isinstance(obj, dict):
            obj = {**obj, "_action": act}
        self._send(json.dumps(obj).encode(), "application/json", code)
        if act:
            record_action(act["target"], act["action"], act["detail"],
                          act["ok"], source="you")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        # keep_blank_values so an explicitly empty parameter is distinguishable
        # from an absent one -- clearing a schedule's scope back to "every unit"
        # is `scope=`, which would otherwise vanish before it was read.
        q = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        one = lambda k, d=None: (q.get(k) or [d])[0]
        given = lambda k: k in q

        if url.path == "/api/summary":
            con = store.connect()
            try:
                if one("from") and one("to"):
                    t0, t1 = float(one("from")), float(one("to"))
                    label = one("label", "Custom range")
                else:
                    t0, t1, label = energy.resolve_range(one("range", "today"))
                self._json(energy.snapshot(con, t0, t1, label))
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/backfill":
            con = store.connect()
            try:
                with _lock:
                    results = backfill.run(con, force=one("force") == "1")
                self._json({"results": results})
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            finally:
                con.close()

        elif url.path == "/api/comparison":
            con = store.connect()
            try:
                self._json(analysis.comparison(
                    con, change=one("change"), new_units=(
                        one("new").split(",") if one("new") else None)))
            except Exception as exc:
                self._json({"available": False, "reason": str(exc)})
            finally:
                con.close()

        elif url.path == "/api/repair":
            con = store.connect()
            try:
                with _lock:
                    self._json({"results": repair.run(con, one("dry") == "1")})
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            finally:
                con.close()

        elif url.path == "/api/identify":
            con = store.connect()
            try:
                act = one("action")
                if act == "start":
                    self._json({"ok": True,
                                **identify.start(con, one("unit"), one("label"))})
                elif act == "confirm":
                    self._json({"ok": True,
                                "bindings": identify.confirm(con, one("pin"))})
                elif act == "stop":
                    identify.stop(con)
                    self._json({"ok": True, "detail": "stopped watching"})
                else:
                    self._json({"watching": identify.check(con),
                                "bindings": identify.known_bindings(con),
                                "wanted": list(identify.WANTED),
                                "units": [d["name"] for d in config.DEVICES]})
            except Exception as exc:
                self._json({"ok": False,
                            "detail": f"{type(exc).__name__}: {exc}"}, 400)
            finally:
                con.close()

        elif url.path == "/api/schedules":
            con = store.connect()
            try:
                act = one("action")
                if act == "add":
                    blynk.coerce(one("field"), one("value"))     # validate first
                    sid = store.add_schedule(
                        con, one("scope", ""), one("field"),
                        float(one("value")), int(one("at")), one("days", "0123456"))
                    self._json({"ok": True, "id": sid,
                                "detail": schedule.describe(
                                    dict(store.schedules(con)[-1]), con)})
                elif act == "edit":
                    rule = next((r for r in store.schedules(con)
                                 if str(r["id"]) == one("id")), None)
                    if not rule:
                        self._json({"ok": False, "detail": "no such rule"}, 404)
                        return
                    # Only the fields actually supplied change; the rest keep
                    # what the rule already had. Validation uses the resulting
                    # pair, so editing just the time cannot leave a rule whose
                    # value was never checked against its field.
                    field = one("field") or rule["field"]
                    raw = one("value")
                    value = rule["value"] if raw in (None, "") else raw
                    blynk.coerce(field, value)
                    changes = {"field": field, "value": float(value)}
                    if given("scope"):
                        changes["scope"] = one("scope", "")
                    if one("at"):
                        changes["at_min"] = int(one("at"))
                    if one("days"):
                        changes["days"] = one("days")
                    # An edited rule has not run in its new form, so let it fire
                    # today if its new time is still ahead.
                    changes["last_run"] = None
                    store.update_schedule(con, rule["id"], **changes)
                    updated = next(r for r in store.schedules(con)
                                   if r["id"] == rule["id"])
                    self._json({"ok": True, "id": rule["id"],
                                "detail": schedule.describe(updated, con)})

                elif act == "delete":
                    store.delete_schedule(con, one("id"))
                    self._json({"ok": True, "detail": f"rule {one('id')} removed"})
                elif act == "toggle":
                    store.update_schedule(con, one("id"),
                                          enabled=1 if one("on") == "1" else 0)
                    self._json({"ok": True,
                                "detail": f"rule {one('id')} "
                                          f"{'enabled' if one('on')=='1' else 'disabled'}"})
                elif act == "run":
                    rule = next((r for r in store.schedules(con)
                                 if str(r["id"]) == one("id")), None)
                    if not rule:
                        self._json({"ok": False, "detail": "no such rule"}, 404)
                        return
                    res = schedule.apply(con, rule, blynk.control)
                    self._json({"ok": not res["failed"], **res,
                                "detail": f"{len(res['applied'])} of "
                                          f"{res['units']} units"})
                else:
                    rules = [dict(r, text=schedule.describe(r, con))
                             for r in store.schedules(con)]
                    self._json({"rules": rules,
                                "next": schedule.next_runs(con),
                                "scopes": ([""] +
                                    [d["name"] for d in config.DEVICES] +
                                    [f"floor:{f}" for f in
                                     sorted(set(store.get_floors(con).values()))]),
                                "controls": {k: {"accepts": v.get("choices")
                                                 or list(v["clamp"])}
                                             for k, v in blynk.CONTROLS.items()}})
            except ValueError as exc:
                self._json({"ok": False, "reason": "bad value",
                            "detail": str(exc)}, 400)
            except Exception as exc:
                self._json({"ok": False,
                            "detail": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/filter-history":
            con = store.connect()
            try:
                with _lock:
                    results = backfill.fetch_filter_history(
                        con, force=one("force") == "1")
                learned = filters.learned_threshold(con, int(time.time()))
                self._json({"ok": any(r["status"] == "ok" for r in results),
                            "results": results,
                            "threshold": learned,
                            "detail": (f"Indicator trips at about {round(learned)} "
                                       f"hours of runtime."
                                       if learned else
                                       "No indicator flip found yet, so the trip "
                                       "point is still unknown.")})
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/backup":
            con = store.connect()
            try:
                if one("run") == "1":
                    self._json(backup.run(con, force=True))
                else:
                    self._json(backup.status(con))
            except Exception as exc:
                self._json({"ok": False,
                            "detail": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/guard":
            con = store.connect()
            try:
                if one("auto") in ("0", "1"):
                    store.set_meta(con, "auto_guard", one("auto"))
                if one("unit") and one("on") in ("0", "1"):
                    r = guard.set_enabled(con, one("unit"), one("on") == "1")
                    self._json({"ok": True, **r,
                                "detail": f"guarding {'on' if r['enabled'] else 'off'} "
                                          f"for {r['unit']}"})
                    return
                self._json({
                    "enabled": guard.guarding_enabled(con),
                    # Two switches are ANDed here. Reporting only the result
                    # leaves a checkbox that refuses to turn on with no way to
                    # say why, so both are sent and the page explains.
                    "deployment": config.GUARD_ENABLED,
                    "paused": store.get_meta(con, "auto_guard", "1") != "1",
                    "units": guard.report(con),
                    "config": {"margin_f": guard.MARGIN_F,
                               "on_margin_f": guard.ON_MARGIN_F,
                               "min_off_s": guard.MIN_OFF_S,
                               "min_on_s": guard.MIN_ON_S,
                               "max_off_s": guard.MAX_OFF_S},
                })
            except Exception as exc:
                self._json({"ok": False,
                            "detail": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/forecast":
            con = store.connect()
            try:
                p = forecast.project(con)
                # Log the day's figure so the projection can be seen converging,
                # the same way the payback estimate is tracked. Kept here rather
                # than in forecast.project, which is deliberately write-free.
                if p.get("available"):
                    store.log_estimate(con, "month_cost", p["month_total"]["cost"])
                    p["history"] = store.estimate_history(con, "month_cost")
                self._json(p)
            except Exception as exc:
                self._json({"available": False, "reason": str(exc)})
            finally:
                con.close()

        elif url.path == "/api/calibrate":
            con = store.connect()
            try:
                if one("from") and one("to"):
                    t0, t1 = float(one("from")), float(one("to"))
                else:
                    t0 = t1 = None          # calibrate picks its own default span
                self._json(calibrate.report(con, t0, t1))
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/meter":
            con = store.connect()
            try:
                if one("from") and one("to"):
                    t0, t1 = float(one("from")), float(one("to"))
                else:
                    t0, t1, _ = energy.resolve_range(one("range", "24h"))
                self._json(meter.report(con, t0, t1))
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/audit":
            con = store.connect()
            try:
                now = int(time.time())
                want = one("range", "24h")
                if want == "all":
                    first, _ = store.audit_span(con)
                    t0, t1, label = (first or now), now, "Everything"
                elif one("from") and one("to"):
                    t0, t1 = float(one("from")), float(one("to"))
                    label = one("label", "Custom range")
                else:
                    t0, t1, label = energy.resolve_range(want)
                src, tgt = one("source") or None, one("target") or None
                rows = store.audit_events(con, source=src, target=tgt,
                                          since=t0, until=t1, limit=EVENT_LIMIT)
                self._json({
                    "events": rows,
                    "sources": store.audit_sources(con),
                    "targets": [d["name"] for d in config.DEVICES],
                    "range": {"from": int(t0), "to": int(t1), "label": label,
                              "shown": len(rows), "limit": EVENT_LIMIT,
                              "total": store.count_audit(con, source=src, target=tgt,
                                                         since=t0, until=t1)},
                })
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/docs":
            try:
                self._json(api_reference())
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        elif url.path == "/api/unit":
            con = store.connect()
            try:
                name = one("unit")
                if not any(d["name"] == name for d in config.DEVICES):
                    self._json({"error": f"No unit named {name!r}."}, 404)
                    return
                if one("from") and one("to"):
                    t0, t1 = float(one("from")), float(one("to"))
                    label = one("label", "Custom range")
                else:
                    t0, t1, label = energy.resolve_range(one("range", "24h"))
                device = next(d for d in config.DEVICES if d["name"] == name)
                d = energy.unit_detail(con, name, t0, t1, label,
                                       live=blynk.read_one(device) or {})
                d["filter"] = filters.report(con).get(name)
                self._json(d)
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/units":
            con = store.connect()
            try:
                act = one("action")
                if act == "add":
                    name = store.add_unit(con, one("name"), one("token"))
                    config.set_devices(store.units(con))
                    self._json({"ok": True, "detail": f"added {name}"})
                elif act == "remove":
                    r = store.remove_unit(con, one("name"),
                                          purge=one("purge") == "1")
                    config.set_devices(store.units(con))
                    self._json({"ok": True,
                                "detail": f"removed {r['removed']}, "
                                          f"history {r['history']}"})
                elif act == "rename":
                    r = store.rename_unit(con, one("name"), one("to"))
                    config.set_devices(store.units(con))
                    self._json({"ok": True,
                                "detail": f"{r['from']} is now {r['to']}, "
                                          f"{r['rows_moved']} rows moved with it"})
                elif act == "label":
                    lab = store.set_label(con, one("name"), one("label", ""))
                    self._json({"ok": True,
                                "detail": f"{one('name')} is shown as {lab!r}"})
                elif act == "toggle":
                    store.set_unit_enabled(con, one("name"), one("on") == "1")
                    config.set_devices(store.units(con))
                    self._json({"ok": True,
                                "detail": f"{one('name')} "
                                          f"{'enabled' if one('on')=='1' else 'disabled'}"})
                else:
                    rows = store.unit_rows(con)
                    fw = store.firmware(con)
                    for r in rows:
                        r["firmware"] = fw.get(r["name"], {})
                    # "Behind" is judged against the newest build actually seen
                    # on this account, not a number written down here: Windmill
                    # ships what it ships, and the fleet is the only evidence of
                    # what current means.
                    seen = sorted({(f.get("version") or "") for f in fw.values()
                                   if f.get("version")})
                    newest = seen[-1] if seen else None
                    for r in rows:
                        v = (r["firmware"] or {}).get("version")
                        r["outdated"] = bool(newest and v and v != newest)
                    self._json({"units": rows, "newest_firmware": newest,
                                "live": [d["name"] for d in config.DEVICES]})
            except ValueError as exc:
                self._json({"ok": False, "reason": "bad value",
                            "detail": str(exc)}, 400)
            except Exception as exc:
                self._json({"ok": False,
                            "detail": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/floors":
            con = store.connect()
            try:
                if one("unit"):
                    if not any(d["name"] == one("unit") for d in config.DEVICES):
                        self._json({"ok": False, "detail": "Unknown unit."}, 404)
                        return
                    store.set_floor(con, one("unit"), one("floor", ""))
                floors = store.get_floors(con)
                self._json({"ok": True, "floors": floors,
                            "names": sorted(set(floors.values()))})
            except Exception as exc:
                self._json({"ok": False, "detail": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/floor/control":
            con = store.connect()
            try:
                names = (store.units_on_floor(con, one("floor")) if one("floor")
                         else [d["name"] for d in config.DEVICES])
                devices = devices_named(names)
                if not devices:
                    self._json({"ok": False, "detail": "No units on that floor."}, 404)
                    return
                field, value = one("field"), one("value")
                blynk.coerce(field, value)     # validate once before fanning out
                results = in_parallel(
                    devices, lambda d: blynk.control(d, field, value))
                for name, r in results.items():
                    if r.get("ok"):
                        store.save_settings(con, name, {r["pin"]: r["value"]},
                                            "dashboard")
                self._json(bulk_summary(results, f"set {field}"))
            except ValueError as exc:
                self._json({"ok": False, "reason": "bad value",
                            "detail": str(exc)}, 400)
            except Exception as exc:
                self._json({"ok": False, "detail": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/resync-all":
            con = store.connect()
            try:
                names = (store.units_on_floor(con, one("floor")) if one("floor")
                         else [d["name"] for d in config.DEVICES])
                devices = devices_named(names)
                if not devices:
                    self._json({"ok": False, "detail": "No units to resync."}, 404)
                    return
                # each opens its own connection: sqlite objects are not safe to
                # share across the threads this fans out into
                def one_resync(d):
                    c = store.connect()
                    try:
                        return do_resync(c, d, "manual resync (bulk)")
                    finally:
                        c.close()
                self._json(bulk_summary(in_parallel(devices, one_resync), "resync"))
            except Exception as exc:
                self._json({"ok": False, "detail": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/sync":
            con = store.connect()
            try:
                if one("auto") in ("0", "1"):
                    store.set_meta(con, "auto_resync", one("auto"))
                if one("overrun") in ("0", "1"):
                    store.set_meta(con, "auto_overrun", one("overrun"))
                if one("cooldown"):
                    store.set_meta(con, "overrun_cooldown",
                                   max(COOLDOWN_MIN, min(COOLDOWN_MAX,
                                                         int(float(one("cooldown"))))))
                now = int(time.time())
                over = overrun.report(con, now)
                # The log is filtered by range like the charts are. "all" is
                # its own case rather than a very long range, so the label
                # stays honest when the log is younger than the range asked for.
                want = one("range", "24h")
                if want == "all":
                    first, _ = store.sync_events_span(con)
                    ev_from, ev_to, ev_label = (first or now), now, "Everything"
                elif one("from") and one("to"):
                    ev_from, ev_to = float(one("from")), float(one("to"))
                    ev_label = one("label", "Custom range")
                else:
                    ev_from, ev_to, ev_label = energy.resolve_range(want)
                events = store.sync_events(con, since=ev_from, until=ev_to, limit=EVENT_LIMIT)
                total = store.count_sync_events(con, since=ev_from, until=ev_to)
                self._json({
                    "auto": auto_resync_enabled(con),
                    "overrun_auto": auto_overrun_enabled(con),
                    "now": now,
                    "config": {
                        "interval_s": WATCH_INTERVAL,
                        "wedged_s": WEDGED_S,
                        "resync_cooldown_s": RESYNC_COOLDOWN,
                        "resync_max": RESYNC_MAX,
                        "overrun_window_s": overrun.WINDOW_S,
                        "overrun_cooldown_s": overrun_cooldown(con),
                        "cooldown_min_s": COOLDOWN_MIN,
                        "cooldown_max_s": COOLDOWN_MAX,
                        "overrun_max": OVERRUN_MAX,
                        "overcool_margin_f": overrun.MARGIN_F,
                        "overcool_frac": overrun.OVERCOOL_FRAC,
                    },
                    "units": {d["name"]: watchdog_status(con, d, over, now)
                              for d in config.DEVICES},
                    "filters": filters.report(con, now),
                    "events": events,
                    "events_range": {"from": int(ev_from), "to": int(ev_to),
                                     "label": ev_label, "shown": len(events),
                                     "total": total, "limit": EVENT_LIMIT},
                })
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/control":
            name = one("unit")
            device = next((d for d in config.DEVICES if d["name"] == name), None)
            if not device:
                self._json({"ok": False, "reason": "unknown unit",
                            "detail": f"No unit named {name!r}."}, 404)
            else:
                con = store.connect()
                try:
                    result = blynk.control(device, one("field"), one("value"))
                    if result["ok"]:
                        # a deliberate change is the strongest statement of what
                        # this unit should be set to, so it goes on file at once
                        store.save_settings(con, name,
                                            {result["pin"]: result["value"]},
                                            "dashboard")
                        # ...and it ends the post-resync quiet window: someone
                        # has just said what they want, so there is no longer
                        # any reason to distrust what the unit reports.
                        store.mark_settling(con, name, 0)
                    self._json({"unit": name, **result},
                               200 if result["ok"] else 409)
                except ValueError as exc:
                    self._json({"ok": False, "reason": "bad value",
                                "detail": str(exc)}, 400)
                except Exception as exc:
                    self._json({"ok": False, "reason": "failed",
                                "detail": f"{type(exc).__name__}: {exc}"}, 500)

        elif url.path == "/api/resync":
            name = one("unit")
            device = next((d for d in config.DEVICES if d["name"] == name), None)
            if not device:
                self._json({"ok": False, "reason": "unknown unit",
                            "detail": f"No unit named {name!r}."}, 404)
            else:
                con = store.connect()
                try:
                    self._json({"unit": name, **do_resync(con, device, "manual resync")})
                except Exception as exc:
                    self._json({"ok": False, "reason": "failed",
                                "detail": f"{type(exc).__name__}: {exc}"}, 500)
                finally:
                    con.close()

        elif url.path == "/api/rate":
            con = store.connect()
            try:
                if one("set"):
                    store.set_meta(con, "rate_per_kwh", float(one("set")))
                self._json({"rate": energy.rate(con)})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            finally:
                con.close()

        else:
            self._send((config.WEB / "index.html").read_bytes(),
                       "text/html; charset=utf-8")


def main():
    """Start the sampler, the maintainer, the watchdog and the server."""
    tee = start_file_log()
    con = store.connect()
    if store.get_meta(con, "rate_per_kwh") is None:
        store.set_meta(con, "rate_per_kwh", config.RATE_PER_KWH)
    imported = migrate_jsonl(con)
    if imported:
        log("INFO", "startup", None, f"imported {imported} readings from samples.jsonl")
    # Units live in the database. Import anything still defined in .env that the
    # database has not seen, then run from the database from here on.
    imported = store.seed_units(con, config.ENV_DEVICES)
    if imported:
        log("INFO", "startup", None,
            f"imported {len(imported)} unit(s) from .env into the database",
            ", ".join(imported))
    config.set_devices(store.units(con))
    if not config.DEVICES:
        log("WARN", "startup", None, "no units configured",
            "add one on the Controls tab, or set WINDMILL_TOKEN_<ROOM> in .env")

    seeded = seed_defaults(con)
    if seeded:
        log("INFO", "startup", None,
            f"seeded defaults ({blynk.settings_label(blynk.DEFAULTS)})",
            ", ".join(seeded))
    banner(con)
    con.close()

    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=maintainer, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    if config.BACKUP_ENABLED:
        threading.Thread(target=backer, daemon=True).start()
    if RELOAD:
        threading.Thread(target=reloader, daemon=True).start()
    try:
        httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    except OSError as exc:
        # "Address already in use" as a traceback tells you nothing you can act
        # on, and it is by far the most common way starting this fails -- almost
        # always a copy already running. Say which copy.
        if getattr(exc, "errno", None) not in (48, 98):     # EADDRINUSE
            raise
        log("ERR", "startup", None, f"port {PORT} is already in use",
            "another copy of the dashboard is probably running")
        holder = _who_has_port(PORT)
        if holder:
            print(f"\n  {_c('already running:', 'dim')} {holder}")
        print(f"\n  Stop it first, or start this one elsewhere:\n"
              f"    WINDMILL_PORT=8788 bash run.sh\n")
        raise SystemExit(1)

    log("INFO", "startup", None,
        f"listening on {BIND}:{PORT} — actions appear here as you take them")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("INFO", "shutdown", None, "stopped")


if __name__ == "__main__":
    main()
