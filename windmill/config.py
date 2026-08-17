"""Load Windmill config from .env (no third-party deps)."""
import os
import pathlib

#: Where the code lives, and where its bundled assets are. Never written to --
#: in a container this is a read-only image layer.
PKG = pathlib.Path(__file__).parent
WEB = PKG / "web"

#: Where *state* lives: the database, backups, the .env file. Separated from the
#: code so a container can mount one writable volume and leave the image alone.
#: Defaults to the repo root, which is where everything sat before this split,
#: so an existing checkout keeps working untouched.
DATA = pathlib.Path(os.environ.get("WINDMILL_DATA") or PKG.parent).resolve()

ENV = pathlib.Path(os.environ.get("WINDMILL_ENV") or (DATA / ".env"))
SAMPLES = DATA / "samples.jsonl"

#: Kept as an alias: plenty of call sites still say config.ROOT, and for a
#: normal checkout it means exactly what it always did.
ROOT = DATA


def _load_env():
    env = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("WINDMILL_")})
    return env


_ENV = _load_env()

HOST = _ENV.get("WINDMILL_HOST", "dashboard.windmillair.com")

#: Fallback only -- the live rate is stored in the database and edited in the UI.
RATE_PER_KWH = float(_ENV.get("WINDMILL_RATE_PER_KWH", 0.105))

#: Location for the degree-day normalisation in analysis.py. Set these to
#: where the units actually are -- the default is only a placeholder, and
#: weather normalising against the wrong place is worse than not doing it.
LAT = float(_ENV.get("WINDMILL_LAT", 40.7128))
LON = float(_ENV.get("WINDMILL_LON", -74.0060))

#: Optional: describe a change to the system so the page can compare before/after.
CHANGE_DATE = _ENV.get("WINDMILL_CHANGE_DATE", "") or None
NEW_UNITS = [s.strip() for s in _ENV.get("WINDMILL_NEW_UNITS", "").split(",") if s.strip()]

#: Units physically moved rooms on this date, so history before it belongs to a
#: different room than the device is named for now.
MOVED_ON = _ENV.get("WINDMILL_MOVED_ON", "") or None

#: "<device name>:<room it was in before MOVED_ON>", comma separated.
PRIOR_ROOMS = dict(
    part.split(":", 1) for part in _ENV.get("WINDMILL_PRIOR_ROOMS", "").split(",")
    if ":" in part
)

#: Restart serve.py when a .py file changes. On by default; set
#: WINDMILL_RELOAD=0 to leave a long-running instance alone.
RELOAD = _ENV.get("WINDMILL_RELOAD", "1") != "0"


def _pick(value, names, fallback):
    """Accept either the name ('cool') or the raw code ('1')."""
    v = str(value).strip().lower()
    for code, name in names.items():
        if v == name.lower() or v == str(code):
            return code
    return fallback


#: What every unit should be set to when nothing better is known: on, cooling,
#: fan on auto. Target temperature is deliberately not part of this -- it is a
#: per-room preference, so a default would be a wrong answer for most rooms.
DEFAULT_POWER = 0 if _ENV.get("WINDMILL_DEFAULT_POWER", "on").lower() in (
    "0", "off", "false") else 1
DEFAULT_MODE = _pick(_ENV.get("WINDMILL_DEFAULT_MODE", "cool"),
                     {0: "Fan", 1: "Cool", 2: "Eco"}, 1)
DEFAULT_FAN = _pick(_ENV.get("WINDMILL_DEFAULT_FAN", "auto"),
                    {0: "Auto", 1: "Low", 2: "Medium", 3: "High"}, 0)

#: Where to send the things this tool cannot fix by itself. All optional; with
#: none set, alerts are computed and shown on the page but not delivered.
ALERT_WEBHOOK = _ENV.get("WINDMILL_ALERT_WEBHOOK", "") or None
ALERT_EMAIL = _ENV.get("WINDMILL_ALERT_EMAIL", "") or None
ALERT_DESKTOP = _ENV.get("WINDMILL_ALERT_DESKTOP", "") not in ("", "0", "false")

SMTP_HOST = _ENV.get("WINDMILL_SMTP_HOST", "") or None
SMTP_PORT = int(_ENV.get("WINDMILL_SMTP_PORT", "587") or 587)
SMTP_USER = _ENV.get("WINDMILL_SMTP_USER", "") or None
SMTP_PASS = _ENV.get("WINDMILL_SMTP_PASS", "") or None
SMTP_FROM = _ENV.get("WINDMILL_SMTP_FROM", "") or None

#: Snapshots of windmill.db. The vendor keeps 30 days; everything older exists
#: only here, so a backup is the only thing standing between a year of history
#: and a disk failure.
BACKUP_DIR = _ENV.get("WINDMILL_BACKUP_DIR", "") or str(DATA / "backups")
BACKUP_KEEP = int(_ENV.get("WINDMILL_BACKUP_KEEP", "14") or 14)
BACKUP_INTERVAL_H = float(_ENV.get("WINDMILL_BACKUP_INTERVAL_H", "20") or 20)
BACKUP_ENABLED = _ENV.get("WINDMILL_BACKUP_ENABLED", "1") != "0"

#: Bar Eco outright. On these units Eco is not a setting anyone chose -- they
#: drop into it on their own and after most writes, and it is the mode that
#: overcools. Barred rather than merely corrected: the give-up limit that keeps
#: the watchdog from fighting faulty hardware forever is right for ordinary
#: drift, but it leaves a unit sitting in Eco for the rest of the hour.
NO_ECO = _ENV.get("WINDMILL_NO_ECO", "1") != "0"

#: How the guard stops a unit cooling: "fan" (default) or "off". Fan-only stops
#: the compressor while leaving the blower running, which keeps the temperature
#: sensor -- mounted behind the intake grille -- reading room air rather than a
#: cold coil. See guard.METHOD for the measurements behind that.
GUARD_METHOD = _ENV.get("WINDMILL_GUARD_METHOD", "fan")

#: The software thermostat guard switches a unit off when its own thermostat
#: will not. Off by default: it drives real hardware, and it is only warranted
#: for units whose firmware demonstrably misbehaves.
GUARD_ENABLED = _ENV.get("WINDMILL_GUARD", "") not in ("", "0", "false")

#: Where the action log is kept, alongside the terminal. Screen output is
#: ephemeral and easy to lose; the log is how you find out what happened while
#: you were not watching.
LOG_DIR = _ENV.get("WINDMILL_LOG_DIR", "") or str(DATA / "logs")
LOG_KEEP = int(_ENV.get("WINDMILL_LOG_KEEP", "14") or 14)
LOG_ENABLED = _ENV.get("WINDMILL_LOG", "1") != "0"

#: The telemetry database. A container points this at its mounted volume.
DB_PATH = _ENV.get("WINDMILL_DB", "") or str(DATA / "windmill.db")

#: Address to listen on. Loopback by default: these endpoints switch real ACs on
#: and off with no authentication, so exposing them is a deliberate act. A
#: container must bind 0.0.0.0 -- the published port is what limits reach there.
HOST_BIND = _ENV.get("WINDMILL_BIND", "127.0.0.1")
PORT = int(_ENV.get("WINDMILL_PORT", "8787") or 8787)

#: Datastream carrying instantaneous watts. Confirmed as Windmill's "Power"
#: stream by correlating its CSV export against live pin reads.
POWER_PIN = _ENV.get("WINDMILL_POWER_PIN", "v15")

#: [{"name": "Living Room", "token": "..."}].
#:
#: Units used to live only here, which made adding one an edit-and-restart job.
#: They are now kept in the database and this is only the seed: at startup the
#: server imports anything defined here that the database does not already know,
#: then replaces the contents of this list with what the database holds.
#:
#: It stays a module-level list, mutated in place rather than rebound, because
#: fifteen modules iterate `config.DEVICES` and all of them should see a unit
#: added through the dashboard without being restarted or rewritten.
DEVICES = [
    {"name": k[len("WINDMILL_TOKEN_"):].replace("_", " ").title(), "token": v}
    for k, v in _ENV.items()
    if k.startswith("WINDMILL_TOKEN_") and v and not v.startswith("your-")
]

#: What was in .env, kept separately so the one-time import can tell the
#: difference between "defined in the file" and "already in the database".
ENV_DEVICES = list(DEVICES)


def set_devices(devices):
    """Replace the live unit list in place. Called once at startup and again
    whenever a unit is added, removed or renamed."""
    DEVICES[:] = [{"name": d["name"], "token": d["token"]} for d in devices]
    return DEVICES


def device(name):
    return next((d for d in DEVICES if d["name"] == name), None)

#: Starting with none is no longer fatal -- units can be added from the
#: dashboard, so an empty list is a first run rather than a broken install.
#: The server says so at startup instead of refusing to boot.
