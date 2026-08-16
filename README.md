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

## Temperature

Outdoor temperature is overlaid on the energy chart as a background wash —
warmer periods shade darker — and charted properly against the average indoor
reading in its own panel below, which also reports the correlation between
outdoor temperature and energy use for the selected range.

The overlay is deliberately **not** a second y-axis. With two scales, whether
the curves appear to track each other is decided by the scaling rather than the
data, and the same numbers can be made to show energy leading or lagging
temperature. Encoding temperature as background intensity keeps one axis, so no
apparent crossing can be manufactured; exact values live in the panel below. Indoor history
is backfilled from the Room Temperature datastream; outdoor comes from Open-Meteo
hourly archive, cached permanently in SQLite.

## Closing gaps in the history

```bash
python3 repair.py --dry-run     # report what it would do
python3 repair.py               # fix
```

`serve.py` runs this hourly on its own, and the **Repair gaps** button triggers
it on demand. Gaps appear for two reasons that need opposite treatment:

- **The export quota was exhausted when we asked.** The data still exists on
  Windmill's side, so retrying recovers it *exactly*. This is never estimated —
  it waits.
- **The unit stopped reporting while still running.** No retry will ever produce
  it; the cloud never received it. Only the unit's own runtime counter can
  recover it.

So repair always attempts a real fetch first and only reconstructs what a fetch
cannot return. Leave `serve.py` running and gaps close by themselves as quotas
reset and units reconnect.

### Estimate tiers

Every gap gets filled, but not every fill is worth the same, so each hour records
*how* it was produced and the weaker methods are replaced first:

| Method | Magnitude comes from | Used when |
|---|---|---|
| *(measured)* | the unit itself | always preferred |
| `runtime` | the unit's own cumulative runtime counter | it ran while disconnected |
| `idle` | the unit's own median standby draw | the counter shows it never ran |
| `cohort` | units installed alongside it | no counter reachable at all |

`cohort` is the weakest — its magnitude comes entirely from other units — so it
is the first thing overwritten. An estimate is never downgraded: a cohort guess
cannot overwrite a runtime reconstruction.

### Real data always wins

Estimates are placeholders. Any hour holding an estimate is overwritten the
moment a real fetch returns that hour, and the `estimated` flag and `method` are
cleared with it. Nothing measured is ever overwritten by an estimate. This is
verified by corrupting a known-good hour into a fake estimate, re-fetching, and
asserting the real value returns.

Because estimates propagate into every total, the dashboard reports the
estimated share per unit and the before/after analysis refuses to present a
conclusion without saying how much of each period is reconstructed.

## Reconstructing hours a unit ran without reporting

```bash
python3 gapfill.py "Third Floor Right" --reference "Third Floor Left" --dry-run
```

A unit can drop off Windmill's cloud for days while still cooling. Those hours are
missing energy, not zero. `gapfill.py` recovers them using the unit's **own**
cumulative Filter Runtime counter, which keeps counting while offline: the
difference across the gap is exactly how many hours it ran, and multiplying by
that unit's measured Wh-per-running-hour gives its energy. A reference unit
supplies only the *shape* used to spread the total across the gap — the magnitude
never depends on it.

Every reconstructed hour is stored with `estimated = 1` and reported separately in
the UI, so it can never be mistaken for a measurement. If the runtime counter is
unavailable it refuses rather than copying a sibling outright, which would invent
a magnitude rather than just a shape.

## Comparing before and after a change

Set `WINDMILL_CHANGE_DATE` and `WINDMILL_NEW_UNITS` in `.env` and the dashboard
grows a **Before vs after** section — a break-even table showing at what outdoor
temperature the two configurations cost the same, and how much you pay above and
below it. `python3 compare.py` prints the same analysis in the terminal.

Raw before/after kWh mostly measures the weather. This regresses daily household
kWh against cooling degree days (daily mean above 65 °F, from Open-Meteo, cached
permanently in SQLite) for each period, then solves for where the two lines cross.

Both surfaces state their own weaknesses rather than leaving you to infer them:

- **Extrapolation.** The break-even usually sits beyond the hottest day in the
  fit. It is labelled when so, with the observed maximum alongside it.
- **Missing units.** A unit without history across the period is not counted as
  zero — it is imputed from its siblings, and every figure it touches becomes a
  low–high band.
- **Thin fits.** R² and sample count are shown, with an explicit warning when the
  data cannot yet carry a confident conclusion.
- **Local climate.** How often break-even is actually reached in your summers,
  and the net effect over a full cooling season.

Note that device *names* follow the physical unit, not the room. If units get
moved between rooms, a unit's history spans both, so per-room comparisons across
a move are not meaningful; whole-house totals still are.

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

`dataStreamId` in the export API is **not** the virtual pin number — id 15 is
"Protocol Failure Count", which is pin `v31`. The names below are Windmill's own,
read from the CSV export headers; each was bound to a pin by correlating exported
values against live reads.

| Export id | Windmill's name | Pin |
|---|---|---|
| 1 | Power Switch | `v0` |
| 2 | Room Temperature | `v1` |
| 3 | Set Temperature | `v2` |
| 7 | Power (watts) | `v15` |
| 9 | Filter Runtime (hours) | `v7` |
| 14 | WiFi RSSI | `v30` |
| 15 | Protocol Failure Count | `v31` |
| 16 | Mode — `0` fan, `1` cool, `2` eco | `v3` |
| 17 | Fan Speed — `0` auto, `1` low, `2` med, `3` high | `v4` |
| 18 | Energy | `v16` |
| 8 | Night Mode | unbound |
| 11 | Beeping Mode | unbound |
| 12 | Filter Change Indicator | unbound |

IDs stop at 18; nothing above it exists. Only these report history — the rest of
the pins are live-only and shown raw in the **All metrics** table.

`v15` = Power was confirmed by pulling that export at minute granularity and
matching it against logged pin values. `v0`–`v4` independently agree with the
community Home Assistant integration
([bzellman/WindmillAC](https://github.com/bzellman/WindmillAC)).

**Three names remain unbound.** Night Mode, Beeping Mode and Filter Change
Indicator are all `0` on every unit right now, so they cannot be told apart from
each other or from `v5`, `v6`, `v8`, `v12`, `v17`, `v116`, `v117`. The exception
is `v11`, which is `1` on exactly three units and constant — a per-unit setting,
most likely one of these three. Toggling one setting in the Windmill app and
re-reading would bind it immediately.

**`v16` is "Energy"**, but the accumulation window is unclear: it does not match
energy-so-far-this-hour, and it has been observed *decreasing*, so it is not
cumulative since install. This project does not use it — kWh is integrated from
`v15` instead.

Still unidentified: `v5`, `v6`, `v8`, `v11`, `v12`, `v17`, `v100`, `v110`–`v113`,
`v116`, `v117`. `v100` and `v110`–`v113` sit at `3` on most units with `v113` at
`0` on two, which looks like a group of per-unit settings rather than telemetry.

## Security

**Device tokens are credentials.** Anyone holding one can read *and control* that
AC unit — no account login required. `.env` is gitignored; keep it that way, and
don't paste tokens into issues, chats, or screenshots.

This project only ever issues `GET` requests and never changes a unit's settings.

## Unofficial

Not affiliated with, endorsed by, or supported by Windmill. It depends on an
undocumented API that they can change or close at any time.
