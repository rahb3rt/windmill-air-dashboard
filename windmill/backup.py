"""Snapshot windmill.db, verify the snapshot, and keep the last N.

Windmill/Blynk keeps 30 days of history and nothing before that. Everything
older than 30 days exists in exactly one place -- the SQLite file next to this
module -- and cannot be re-fetched at any price. Losing that file loses the
only copy of the thing this whole tool was built to accumulate, so a backup is
worth more here than any feature.

Why VACUUM INTO rather than copying the file:

  The database runs in WAL mode with a live writer (the sampler writes every
  30s). At any instant the committed state is spread across windmill.db and
  windmill.db-wal, and `cp` copies them at different moments -- the result is a
  file that opens fine and is quietly missing or tearing the most recent
  commits. VACUUM INTO runs inside a read transaction, so the snapshot is the
  database exactly as of one consistent point in time, taken without stopping
  the writer. It also compacts as it goes, so the snapshot is smaller than the
  original and arrives with no -wal or -shm to lose.

  The cost is that VACUUM INTO refuses to overwrite an existing file. That is
  handled by writing to a temporary name in the same directory and promoting it
  with os.replace only once it has been verified -- so a failed or corrupt
  snapshot can never replace the good one already sitting under today's name,
  and no half-written file is ever left where a restore would find it.

Why verify:

  A backup that has never been read back is a guess. The failure this catches
  is the quiet one -- a truncated write, a full disk, a snapshot of a database
  that was already damaged -- where the file exists, has a plausible size, and
  is worthless. So every snapshot is reopened read-only, integrity-checked,
  and checked for the expected tables and a row count close to the live one
  before it is allowed to count as a backup.

Nothing here raises. A backup failing is bad; a backup failure taking down the
sampler that is producing the data is worse. Every entry point returns a result
dict with an `ok` flag and a human-readable `detail`.
"""
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import time

from . import config, store


#: Where snapshots live. Read through getattr so the settings below start
#: working the moment they are declared in config.py, with sane defaults until
#: then (see the module notes in the README for the env var names).
DIR = pathlib.Path(getattr(config, "BACKUP_DIR", None) or (config.ROOT / "backups"))

#: How many snapshots to keep. Two weeks is chosen against the thing being
#: guarded: silent corruption noticed late. A week is not enough time to notice.
KEEP = int(getattr(config, "BACKUP_KEEP", 14))

#: Don't take another snapshot if one succeeded this recently. Slightly under a
#: day, so a caller running hourly produces one backup per day and never skips a
#: day just because yesterday's ran a few minutes later.
MIN_INTERVAL_S = int(float(getattr(config, "BACKUP_INTERVAL_H", 20)) * 3600)

#: One snapshot per day, named for the day. Rotation only ever deletes files
#: matching this exactly -- never a glob -- so anything else a person put in the
#: backup directory (a manual copy, a note, an export) is untouchable.
PREFIX = "windmill-"
NAME_RE = re.compile(r"^windmill-\d{4}-\d{2}-\d{2}\.db$")
STAMP = "%Y-%m-%d"

#: Tables a real snapshot must contain. Deliberately the ones holding data that
#: cannot be recomputed or refetched: raw samples, the rollup built from them,
#: and the state keys. A snapshot missing any of these is not the database.
REQUIRED_TABLES = ("readings", "hourly", "meta")

#: A snapshot with less than this share of the live `readings` count is treated
#: as failed. Not 1.0: the live count is taken before the snapshot and the
#: sampler keeps writing, so exact equality is not expected -- this catches a
#: snapshot that lost data, not one that is a few rows behind.
MIN_ROW_FRACTION = 0.99

#: Refuse to start if the free space is under this multiple of the database
#: size. Filling the disk would take down the sampler, which is a far worse
#: outcome than skipping one backup.
SPACE_FACTOR = 1.5

#: meta keys: when the last snapshot succeeded, and where it landed.
META_LAST = "backup:last"
META_PATH = "backup:path"


def _db_path(con):
    """The file `con` is actually attached to.

    Asked of the connection rather than assumed from config, so a snapshot is
    always of the database the caller is really using -- which is what makes
    this testable against a scratch file instead of only the live one.
    """
    for row in con.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return pathlib.Path(row[2])
    return pathlib.Path(store.DB)          # in-memory or otherwise nameless


def is_backup(path):
    """True only for a plain file this module itself would have written."""
    path = pathlib.Path(path)
    return (NAME_RE.fullmatch(path.name) is not None
            and path.is_file() and not path.is_symlink())


def existing(directory=None):
    """Our snapshots in `directory`, oldest first.

    Sorted by name, not mtime: the name carries the date the snapshot is *of*,
    while mtime changes if the files are ever copied or restored around. What
    rotation should drop is the oldest data, not the least recently touched file.
    """
    directory = pathlib.Path(directory or DIR)
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.iterdir() if is_backup(p)), key=lambda p: p.name)


def snapshot_path(when=None, directory=None):
    """Where today's snapshot belongs."""
    stamp = time.strftime(STAMP, time.localtime(when or time.time()))
    return pathlib.Path(directory or DIR) / f"{PREFIX}{stamp}.db"


def verify(path, expect_rows=None):
    """Reopen a snapshot read-only and decide whether it is really a backup.

    Read-only on purpose: verification must not be able to create, upgrade or
    otherwise touch the thing it is judging, and opening a corrupt file
    read-write is how a bad snapshot gets a fresh journal and starts looking
    healthy.
    """
    path = pathlib.Path(path)
    out = {"ok": False, "tables": [], "rows": None, "detail": ""}
    if not path.exists():
        out["detail"] = "snapshot is missing"
        return out
    con = None
    try:
        # URI mode=ro fails loudly rather than creating an empty database if the
        # path is wrong, which a plain connect() would do silently.
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        check = con.execute("PRAGMA integrity_check(1)").fetchone()
        if not check or check[0] != "ok":
            said = check[0] if check else "nothing"
            out["detail"] = f"integrity_check said {said!r}"
            return out
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        out["tables"] = sorted(names)
        missing = [t for t in REQUIRED_TABLES if t not in names]
        if missing:
            out["detail"] = f"missing table(s): {', '.join(missing)}"
            return out
        rows = con.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        out["rows"] = rows
        if expect_rows:
            if rows < expect_rows * MIN_ROW_FRACTION:
                out["detail"] = (f"only {rows} readings against {expect_rows} live "
                                 f"-- the snapshot lost data")
                return out
        elif rows <= 0:
            out["detail"] = "no readings in the snapshot"
            return out
        out["ok"] = True
        out["detail"] = f"{rows} readings, {len(names)} tables, integrity ok"
        return out
    except Exception as exc:
        out["detail"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        if con is not None:
            con.close()


def rotate(directory=None, keep=None):
    """Delete all but the newest `keep` snapshots. Returns the names removed."""
    keep = KEEP if keep is None else int(keep)
    files = existing(directory)
    removed = []
    if keep < 1 or len(files) <= keep:
        return removed
    for path in files[:len(files) - keep]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            # A file we cannot delete is not a reason to fail a good backup;
            # the next run will try again.
            continue
    return removed


def due(con, now=None, min_interval_s=None):
    """Whether enough time has passed since the last successful snapshot."""
    interval = MIN_INTERVAL_S if min_interval_s is None else int(min_interval_s)
    try:
        last = float(store.get_meta(con, META_LAST, 0) or 0)
    except (TypeError, ValueError):
        last = 0
    return (now or time.time()) - last >= interval, last


def nothing_to_save(con):
    """True when the database holds no telemetry yet.

    A fresh install has nothing worth snapshotting, and reporting that as an
    error makes the first run of a healthy deployment look broken. It is a skip,
    not a failure -- there will be something to save within the minute.
    """
    try:
        return con.execute("SELECT COUNT(*) FROM readings").fetchone()[0] <= 0
    except Exception:
        return False


def run(con, directory=None, keep=None, min_interval_s=None, force=False, now=None):
    """Take, verify, record and rotate one snapshot. Never raises.

    Returns a dict with `ok`, a `status` of ok | skipped | error, and enough
    detail to print a line about it:

        {"ok": True, "status": "ok", "path": "...", "bytes": 1900544,
         "duration_s": 0.12, "removed": [...], "kept": 14, "verify": {...}}
    """
    if nothing_to_save(con):
        return {"ok": True, "status": "skipped", "path": None, "bytes": 0,
                "duration_s": 0.0, "removed": [], "kept": len(existing(directory)),
                "verify": None, "detail": "nothing logged yet, so nothing to save"}

    now = now or time.time()
    started = time.monotonic()
    result = {"ok": False, "status": "error", "path": None, "bytes": 0,
              "duration_s": 0.0, "removed": [], "kept": 0, "verify": None,
              "detail": ""}
    tmp = None
    try:
        ready, last = due(con, now, min_interval_s)
        if not ready and not force:
            files = existing(directory)
            result.update(
                ok=True, status="skipped", kept=len(files),
                path=store.get_meta(con, META_PATH),
                detail=f"backed up {int((now - last) / 3600)}h ago; "
                       f"next in {int((MIN_INTERVAL_S - (now - last)) / 3600)}h")
            return result

        source = _db_path(con)
        target = snapshot_path(now, directory)
        target.parent.mkdir(parents=True, exist_ok=True)

        size = source.stat().st_size
        free = shutil.disk_usage(target.parent).free
        if free < size * SPACE_FACTOR:
            result["detail"] = (f"only {free // 1_000_000} MB free for a "
                                f"{size // 1_000_000} MB database")
            return result

        # Row count first, so verification has something from *before* the
        # snapshot to hold it against.
        expect = con.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

        # Unique per process: two callers racing must not hand each other a
        # half-written temp file, and VACUUM INTO refuses a path that exists.
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        if tmp.exists():
            tmp.unlink()

        # A separate connection, deliberately: VACUUM cannot run inside a
        # transaction, and the caller's connection may be in one.
        src = sqlite3.connect(source, timeout=60)
        try:
            src.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            src.close()

        checked = verify(tmp, expect_rows=expect)
        result["verify"] = checked
        if not checked["ok"]:
            result["detail"] = f"snapshot rejected: {checked['detail']}"
            return result

        # Only now is it allowed to become today's backup. os.replace is atomic
        # within the directory, so a reader never sees a partial file and an
        # earlier good snapshot for today is replaced only by a verified one.
        os.replace(tmp, target)
        tmp = None

        result["bytes"] = target.stat().st_size
        result["path"] = str(target)
        result["removed"] = rotate(directory, keep)
        result["kept"] = len(existing(directory))
        result["ok"] = True
        result["status"] = "ok"
        result["detail"] = (f"{result['bytes'] / 1_000_000:.1f} MB, "
                            f"{checked['rows']} readings")
        store.set_meta(con, META_LAST, int(now))
        store.set_meta(con, META_PATH, str(target))
        return result
    except Exception as exc:
        result["detail"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        # Never leave a rejected or abandoned snapshot behind to be mistaken for
        # a backup or to fill the disk.
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        result["duration_s"] = round(time.monotonic() - started, 3)


def status(con, directory=None):
    """What backups exist right now, for the page and the API. Never raises."""
    try:
        files = existing(directory)
        ready, last = due(con)
        return {
            "ok": True,
            "dir": str(pathlib.Path(directory or DIR)),
            "count": len(files),
            "keep": KEEP,
            "bytes": sum(p.stat().st_size for p in files),
            "newest": files[-1].name if files else None,
            "oldest": files[0].name if files else None,
            "last_run": int(last) or None,
            "due": ready,
            "interval_h": round(MIN_INTERVAL_S / 3600, 1),
        }
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    con = store.connect()
    r = run(con, force="--force" in sys.argv)
    print(f"{r['status']:<8} {r['detail']}")
    if r["removed"]:
        print(f"removed  {', '.join(r['removed'])}")
    print(f"kept     {r['kept']} in {DIR} ({r['duration_s']}s)")
    con.close()
