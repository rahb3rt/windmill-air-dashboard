# Windmill AC dashboard

A local web dashboard for [Windmill Air](https://windmillair.com) AC units — every
unit on one page, compared over any time range, with what each one actually costs you.

Windmill has no official web dashboard and no public API. `dashboard.windmillair.com`
is a white-labeled [Blynk](https://blynk.io) deployment whose HTTP API accepts each
unit's device token directly. That's what this reads.

## Setup

```bash
cp .env.example .env       # then paste in your device tokens
chmod 600 .env
python3 serve.py           # -> http://localhost:8787
```

No dependencies — standard library only. Tokens come from
`dashboard.windmillair.com` → each device → its auth token. One
`WINDMILL_TOKEN_<ROOM>` line per unit; the room name becomes the display name.

On first run it pulls 30 days of history from Windmill and then samples every
unit every 30 seconds into `windmill.db`.

## Time ranges

Presets for today, 24h, 7 days, 30 days, month-to-date, year-to-date and 12
months, plus a custom from/to picker. The page adapts to the range:

| Range | Source | Chart |
|---|---|---|
| ≤ 6 hours | raw 30s samples | watts per unit, line chart |
| longer | hourly rollup | kWh per bucket, stacked by unit |

Buckets widen automatically (hour → 6h → day → week) so a 12-month query returns
a few hundred points rather than millions.

**How far back you can actually go:** Windmill retains **30 days** and exposes no
`year` period, so 30 days is the hard ceiling on backfill. Anything longer only
exists because this tool logged it. Leave `serve.py` running — see
`com.windmill.dashboard.plist` for a launchd agent that survives reboots.

## Rate

Set your electricity rate in the toolbar (`$/kWh`). It's stored in the database
and applied to every range retroactively. `WINDMILL_RATE_PER_KWH` in `.env` is
only the initial default.

## How energy is calculated

The cloud exposes no trustworthy cumulative energy counter, so this doesn't use
one. Watts are sampled and integrated locally with the trapezoid rule. Gaps
longer than 15 minutes — a unit offline, or the logger stopped — are skipped
rather than interpolated, so downtime never invents usage.

Caveats worth knowing:

- kWh is only as good as the sample rate. A compressor cycling faster than 30s
  gets aliased. Lower `SAMPLE_INTERVAL` in `serve.py` if that matters.
- Backfilled hours come from Windmill's hourly *mean* watts, so they're exact on
  energy but flatten short compressor cycles. Locally sampled hours always win
  over backfilled ones for the same hour.
- "Compressor time" is inferred as sustained draw above 100 W, not reported.
- Cost is a flat rate — no time-of-use tiers.

## Storage

SQLite (`windmill.db`), three tables:

- `readings` — sampled datastream values, written **only when they change** plus a
  keyframe every 15 min. AC state is mostly static, so this compresses roughly 18×;
  a year of 30s polling lands in the low millions of rows rather than hundreds of millions.
- `hourly` — per-unit energy per hour. Long ranges read this, never raw samples.
- `exports` — which Blynk exports have already been fetched, so a period already
  pulled is never re-fetched.

### The export rate limit

Blynk allows **72 report exports per device per day**. Exceed it and you get
`HTTP 400: Reports limit reached` for the rest of the day. `backfill.py` records
every fetch, refuses to re-pull a period fetched within the last 6 hours, and
stops at the quota rather than hammering it. The **Backfill** button is safe to
press — it's a no-op when everything is already cached.

## Datastream map

`dataStreamId` in the export API is **not** the virtual pin number. These were
established by exporting each stream and correlating against live pin reads:

| Export id | Name | Pin |
|---|---|---|
| 2 | Room Temperature | `v1` |
| 3 | Set Temperature | `v2` |
| 7 | **Power** (watts) | `v15` |
| 8 | Night Mode | — |
| 9 | Filter Runtime (hours) | `v7` |
| 11 | WiFi RSSI | `v30` |
| 12 | Filter Change Indicator | — |
| 15 | Protocol Failure Count | `v31` |

Control pins, confirmed against the community Home Assistant integration
([bzellman/WindmillAC](https://github.com/bzellman/WindmillAC)):

| Pin | Meaning |
|---|---|
| `v0` | Power — `0` off, `1` on |
| `v1` | Current room temperature (°F) |
| `v2` | Target temperature (°F) |
| `v3` | Mode — `0` fan, `1` cool, `2` eco |
| `v4` | Fan speed — `0` auto, `1` low, `2` medium, `3` high |

`v15` = watts was confirmed by pulling the "Power" export at minute granularity
and matching it against logged pin values. Only these five datastreams report
history; the rest are live-only and shown raw in the **All metrics** table.

Still unidentified: `v5`, `v6`, `v8`, `v11`, `v12`, `v16`, `v17`, `v100`,
`v110`–`v113`, `v116`, `v117`. Note `v16` has been observed *decreasing*, so it is
not a cumulative energy counter.

## Security

**Device tokens are credentials.** Anyone holding one can read *and control* that
AC unit — no account login required. `.env` is gitignored; keep it that way, and
don't paste tokens into issues, chats, or screenshots.

This project only ever issues `GET` requests and never changes a unit's settings.

## Unofficial

Not affiliated with, endorsed by, or supported by Windmill. It depends on an
undocumented API that they can change or close at any time.
