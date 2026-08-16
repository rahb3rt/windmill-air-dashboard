"""Load Windmill config from .env (no third-party deps)."""
import os
import pathlib

ROOT = pathlib.Path(__file__).parent
ENV = ROOT / ".env"
SAMPLES = ROOT / "samples.jsonl"


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

#: Location for the degree-day normalisation in analysis.py (defaults: New Haven, CT)
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

#: Datastream carrying instantaneous watts. Confirmed as Windmill's "Power"
#: stream by correlating its CSV export against live pin reads.
POWER_PIN = _ENV.get("WINDMILL_POWER_PIN", "v15")

#: [{"name": "Living Room", "token": "..."}] — order follows .env
DEVICES = [
    {"name": k[len("WINDMILL_TOKEN_"):].replace("_", " ").title(), "token": v}
    for k, v in _ENV.items()
    if k.startswith("WINDMILL_TOKEN_") and v and not v.startswith("your-")
]

if not DEVICES:
    raise SystemExit(
        "No devices configured. Copy .env.example to .env and add your "
        "WINDMILL_TOKEN_<ROOM> values (find them at dashboard.windmillair.com)."
    )
