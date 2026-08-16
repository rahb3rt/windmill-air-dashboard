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
RATE_PER_KWH = float(_ENV.get("WINDMILL_RATE_PER_KWH", 0.24))

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
