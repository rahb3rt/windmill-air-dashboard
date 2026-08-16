# Windmill AC dashboard

A local web dashboard for [Windmill Air](https://windmillair.com) window AC units —
all units on one page, compared side by side, with a running total of what they cost you.

Windmill has no official web dashboard and no public API. `dashboard.windmillair.com`
is a white-labeled [Blynk](https://blynk.io) IoT deployment, and Blynk's HTTP API
accepts each unit's device token directly. That's what this reads.

## Setup

```bash
cp .env.example .env       # then paste in your device tokens
chmod 600 .env
python3 serve.py           # -> http://localhost:8787
```

No dependencies — standard library only.

Get the tokens from `dashboard.windmillair.com` → each device → its auth token.
One `WINDMILL_TOKEN_<ROOM>` line per unit; the room name in the variable becomes
the display name.

Leave `serve.py` running. It samples every unit every 30s into `samples.jsonl`
and the page refreshes itself every 30s. History only accumulates while it runs.

## What it shows

- **Now** — total watts across the house, per-unit power, mode, fan, room vs target temp
- **Today** — kWh and cost per unit, ranked, with each unit's share of the total
- **Over time** — power draw per unit, where the steps are compressor cycles
- **All metrics** — every datastream each unit reports, named where known, raw where not

Set your electricity rate with `WINDMILL_RATE_PER_KWH` in `.env` (default `0.24`).

## How energy is calculated

The cloud exposes no trustworthy cumulative energy counter, so this doesn't use one.
`V15` is sampled as instantaneous watts and integrated locally with the trapezoid
rule. Gaps longer than 10 minutes — a unit dropping offline, or the logger being
stopped — are skipped rather than interpolated, so downtime never invents usage.

Consequences worth knowing:

- kWh is only as good as the sample rate. A compressor that cycles faster than 30s
  gets aliased. Lower `SAMPLE_INTERVAL` in `serve.py` if you care.
- "Today" means *since the logger started today*, not since midnight. Nothing
  backfills history from before the first run.
- Cost is a flat rate. It won't model time-of-use tiers.

## Datastream map

Confirmed against the community Home Assistant integration
([bzellman/WindmillAC](https://github.com/bzellman/WindmillAC)):

| Pin | Meaning |
|-----|---------|
| `V0` | Power — `0` off, `1` on |
| `V1` | Current room temperature (°F) |
| `V2` | Target temperature (°F) |
| `V3` | Mode — `0` fan, `1` cool, `2` eco |
| `V4` | Fan speed — `0` auto, `1` low, `2` medium, `3` high |

Inferred here by observation, **not confirmed by any vendor documentation**:

| Pin | Reading |
|-----|---------|
| `V15` | Instantaneous watts. Rises and falls rather than accumulating, sits near zero on idle units, jumps to a few hundred when a compressor engages, and scales with unit size. |

Still unidentified: `V5`–`V8`, `V11`, `V12`, `V16`, `V17`, `V30`, `V31`, `V100`,
`V110`–`V113`, `V116`, `V117`. They're all shown raw in the **All metrics** table —
watch one against a known physical change to pin it down. `V16` is a small float
that has been observed *decreasing*, so it is not a cumulative energy counter.

## Security

**Device tokens are credentials.** Anyone holding one can read *and control* that
AC unit — no account login required. `.env` is gitignored; keep it that way, and
don't paste tokens into issues, chats, or screenshots.

This project only ever issues `GET` requests and never changes a unit's settings.

## Unofficial

Not affiliated with, endorsed by, or supported by Windmill. It depends on an
undocumented API that they can change or close at any time.
