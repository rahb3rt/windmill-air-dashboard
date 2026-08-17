# Windmill AC dashboard

A local web dashboard for [Windmill Air](https://windmillair.com) AC units — every
unit on one page, compared over any time range, with what each one actually costs you.

Windmill has no official web dashboard and no public API. `dashboard.windmillair.com`
is a white-labeled [Blynk](https://blynk.io) deployment whose HTTP API accepts each
unit's device token directly. That's what this reads.

## Layout

```
windmill/           the application
  config store blynk          settings, database, upstream client
  energy analysis meter       what it costs, normalised, checked
  calibrate forecast          which rule to trust, where the month lands
  overrun filters guard       faults, filters, the software thermostat
  schedule notify identify    rules, alerts, binding unnamed datastreams
  backup backfill gapfill repair
  server                      HTTP, the watchdog, the action log
  web/index.html              the page
tools/              one-off scripts, not part of the running app
run.sh              start it, on screen and into logs/
Dockerfile          ship it
```

Code and state are kept apart. The package is read-only at runtime; everything
written -- the database, backups, `.env` -- lives under `WINDMILL_DATA`, which
defaults to the repo root and is a mounted volume in a container.

## Setup

```bash
cp .env.example .env       # host + rate; units are added in the app
chmod 600 .env
bash run.sh                # -> http://localhost:8787
```

Everything the app prints goes to the screen *and* to `logs/windmill-<date>.log`,
rotating daily and keeping a fortnight. That is done inside the application
rather than by the launcher, so it happens however you start it -- `run.sh`,
`python3 -m windmill`, launchd, or a container -- and it captures the lines you
most want kept, which are the ones written by code that was not expecting to
fail. Colour is stripped on the way to disk; escape codes are for a terminal and
break `grep` in a file. Set `WINDMILL_LOG=0` for screen only.

### Adding units

Units live in the database, not in `.env`. Add them on the **Settings** tab with
the device token from `dashboard.windmillair.com` -- no restart, nothing to
edit. Any `WINDMILL_TOKEN_*` still in `.env` is imported once on first run and
then ignored, so an existing setup migrates itself.

**Label** changes what the dashboard calls a unit and costs nothing. **Rename**
changes the key its history is stored under, and moves every row keyed to it in
one transaction -- a half-renamed unit would read as two units with half a
history each. Removing a unit keeps its history unless you ask otherwise.

A token is write-only from the dashboard: the API returns the last four
characters and never the value.

## Running in a container

```bash
docker compose up -d        # -> http://127.0.0.1:8787
```

State lives on the `./data` volume: database, backups and `.env`. The image
carries only the package, so an upgrade is a rebuild and nothing is lost.

Published on loopback by default. These endpoints switch real air conditioners
on and off and have **no authentication**, so reaching them from elsewhere on
the network should be a deliberate choice -- put it behind Tailscale or a
reverse proxy rather than binding `0.0.0.0` on the host.

The container sets `TZ` because everything here is local-time aligned -- ranges,
schedules, degree-days -- and under UTC "today" would mean something different
inside the container than out.

On start it prints what it is, what it is doing and where everything lives —
address, upstream host, database and its size, how much history exists, the
units, the rate, and which watchdog switches are on:

```
  Windmill AC dashboard
  listening   http://localhost:8787
  upstream    dashboard.windmillair.com
  database    …/windmill.db (1.7 MB, 22,628 readings)
  history     2026-08-08 20:00 → 2026-08-16 18:06  (7.9 days, last sample just now)
  units       7 — Bedroom, Kitchen, Living Room, …
  rate        $0.105/kWh
  sampling    every 30s
  watchdog    sync every 120s · overrun every 30s · auto-resync on · auto-fix overrun on (cooldown 30 min)
  filters     3 due — Spare Room, Third Floor Left, Third Floor Right
  reload      watching 13 .py files
```

`windmill/server.py` restarts itself when any `.py` in the package changes, so edits take
effect without touching the terminal. A changed file is compiled first — a
half-written file or a syntax error holds the reload and prints what's wrong
rather than exec-ing into a broken process and taking sampling down. `index.html`
is not watched because it is read from disk on every request, so page edits are
already live. Set `WINDMILL_RELOAD=0` to turn this off.

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
exists because this tool logged it. Leave it running — see
`com.windmill.dashboard.plist` for a launchd agent that survives reboots.

## Tabs and toolbars

The page is split into **Controls**, **Watchdog**, **Energy**, **Climate**,
**Analysis**, **Data**, **Schedule**, **Settings**, **Audit** and **API**. Two toolbars serve them, and each tab gets only the
one it can act on: the range picker, the `$/kWh` rate, the unit filter and the
range-dependent stat row appear on the history tabs, while the live switches
(auto-resync, auto-fix overrun, sync all) appear on Controls and Watchdog. A
time range and a price per kWh mean nothing on a page for driving units, and a
tab showing history has nothing to do with an auto-resync switch.

The unit filter deliberately does not apply to the Controls tab. Its chips are
not shown there, so a filtered-out unit would disappear with no visible way to
bring it back.

Charts redraw when their tab becomes visible rather than on load, because an
element sized from `clientWidth` measures zero while hidden.

## Rate

Set your electricity rate in the toolbar (`$/kWh`) on any of the history tabs.
It's stored in the database and applied to every range retroactively.
`WINDMILL_RATE_PER_KWH` in `.env` is only the initial default.

## Temperature

Outdoor temperature is drawn over the energy chart on its own right-hand axis,
and charted properly against the average indoor reading in its own panel below,
which also reports the correlation between outdoor temperature and energy use
for the selected range. Click **Outdoor °F** in the legend to hide the line.

A second y-axis does mean the apparent gap between the curves is partly a choice
of ranges, so the line is drawn to be *followed*, not measured against the bars:
thin, dashed, in neutral ink so it never borrows a series colour and cannot be
mistaken for a unit. Its scale is rounded to whole 5 °F steps rather than fitted
tightly to the data, so its position stays comparable as the range changes, and
exact values live in the panel below.

(An earlier version encoded temperature as a background wash, which avoids the
second scale entirely but proved too faint to follow.) Indoor history
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
cannot return. Leave it running and gaps close by themselves as quotas
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

The headline is a payback figure: **how many hot days a season would need for the
new setup to pay for the mild days it costs you**, given your actual local
climate. When even the hottest day on record still costs more, it says `Never`
and shows the shortfall rather than quoting a temperature that cannot occur.

It is rolling — recomputed from the database on every page refresh, and a daily
snapshot is kept in `estimate_log` so you can watch the figure converge as real
days replace estimates. The first write of each day wins, so refreshing does not
overwrite the day's value.

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

## Units that changed rooms

Device names follow the hardware, so after a unit is moved its older history is
labelled with a room it was not in. Set `WINDMILL_MOVED_ON` and
`WINDMILL_PRIOR_ROOMS` (`Bedroom:Kitchen,...`) and the dashboard labels each unit
by where it actually was for the range on screen:

- A range entirely before the move shows the old room names.
- A range entirely after shows the current names.
- A range spanning the move keeps the current name and adds *"was X before
  <date>"*, since no single label is correct across it.

Units are also dropped from ranges predating their installation. Without that, a
pre-move range lists two units as "Kitchen" — the device that was there then, and
the one named that now. Colour and filtering still key off the device, so a unit
keeps its identity even as its label changes.

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

### Importing a manual export

```bash
python3 import_csv.py --unit Kitchen ~/Downloads/widget_*.csv
```

The dashboard's own widget download beats the HTTP API on every axis: no quota,
and the device's native ~10 second cadence rather than hourly means. When a
unit's quota is exhausted, downloading by hand and importing is the fastest fix.

Values are stored as readings and the hourly rollup is rebuilt from them, exactly
as locally sampled data is — so an import is a real measurement and clears any
estimate covering those hours.

### The export rate limit

Blynk allows **72 report exports per device per day**. Exceed it and you get
`HTTP 400: Reports limit reached` for the rest of the day. `backfill.py` records
every fetch, refuses to re-pull a period fetched within the last 6 hours, and
stops at the quota rather than hammering it. The **Backfill** button is safe to
press — it's a no-op when everything is already cached.

## Controls, sync, and the resync watchdog

Each unit's card carries live controls — power, setpoint, mode, fan — and a sync
line saying whether the unit is actually talking. The controls write through
`/api/control`, which accepts only the four settings above and clamps the
setpoint to 60–86 °F; nothing else on the device is writable from here.

The **API tab** documents all twelve endpoints, their parameters and what they
return, and will run the read-only ones against the live server so the response
shape shown is the one it actually returns today. It renders from `/api/docs`,
which the server builds from the same catalogue the routes are declared with —
so it cannot document a route that does not exist or miss one that does. Calls
that change a setting or drive an AC are listed and labelled but deliberately
not runnable from a documentation page.

The same tab also documents **Windmill's own API** — the six upstream calls this
tool makes, declared in `blynk.py` next to the code that makes them. Windmill
publishes no documentation for any of it, so every line is something established
by calling it and watching what came back, including the traps: `getAll` answers
for a unit that is no longer there, a successful `update` does not mean the
hardware got it, and `dataStreamId` is not the virtual pin number.

The page follows the system light/dark setting by default, with a **Light /
Dark / Auto** switch in the header. Charts read their colours at draw time, so
switching redraws them.

| Endpoint | Does |
|---|---|
| `/api/control?unit=&field=&value=` | one setting on one unit |
| `/api/floor/control?floor=&field=&value=` | one setting across a floor |
| `/api/resync?unit=` | restore one unit to its saved settings |
| `/api/resync-all[?floor=]` | restore a floor, or everything |
| `/api/floors[?unit=&floor=]` | read or assign floor membership |
| `/api/unit?unit=[&range=\|&from=&to=]` | one unit's full series for its own chart |
| `/api/sync[?auto=0\|1][&overrun=0\|1]` | sync status, overrun verdicts, watchdog log, both toggles |
| `/api/audit[?range=&source=&target=]` | every action performed, by you or the watchdog |
| `/api/meter[?range=]` | our integrated kWh against each unit's own counter |
| `/api/schedules[?action=add\|delete\|toggle\|run]` | time-of-day rules |
| `/api/identify[?action=start\|confirm\|stop]` | bind an unnamed pin by watching it move |
| `/api/filter-history[?force=1]` | import filter history from the export API |
| `/api/guard[?auto=0\|1][&unit=&on=]` | the thermostat guard, and which units it may switch |
| `/api/forecast` | where this month lands, with a range and its caveats |
| `/api/calibrate[?range=]` | grade integration rules against the units' own meters |
| `/api/backup[?run=1]` | snapshot the database, or report on snapshots |
| `/api/units[?action=add\|label\|rename\|remove]` | your AC units; tokens never returned |
| `/api/docs` | the catalogue above, as data |

**Why a sync indicator exists at all.** `getAll` answers out of the *cloud's*
copy of each datastream, and that copy outlives the unit's session with it. A
unit that has dropped off still returns a complete, plausible pin dict — last
known values, frozen, with nothing in the response saying so. Left alone, that
turns into invented history: the rollup carries the last wattage forward and
bills you for a compressor that was never running.

`isHardwareConnected` is the only endpoint that distinguishes the two, so every
poll asks for it and stores the answer as a synthetic pin, `c0`. That pin is
what `online` means on the page, and it gates the rollup — an hour a unit spent
disconnected produces no row at all, so `repair.py` sees an honest gap instead
of a confident number.

Three states, because they need three different responses:

| State | Meaning | What fixes it |
|---|---|---|
| **Synced** | Session held, heartbeat moving | — |
| **Not syncing** | Session held, but the unit has gone quiet | **Resync** — re-sends every setting and cycles `v0` in software |
| **Dropped** | No session; nothing sent arrives | Only removing mains power |

**Resync** is the remote stand-in for the unplug/replug ritual, and it works for
the common case: the unit still holds its session but has stopped acting on what
the cloud tells it. It cannot help a **Dropped** unit — nothing reaches those —
so that button is disabled rather than silently doing nothing.

### Settings are kept in the database, not read back off the unit

Every poll records the settings of each *healthy* unit into `unit_settings`,
and a change made here is written there immediately. A resync returns a unit to
whatever that table says it should be, rather than to what the unit itself
reports — by the time a unit is wedged its cloud-side copy is the thing under
suspicion, and restoring from it would happily put back whatever it had drifted
to. Units that are wedged or dropped are skipped when recording, so a failure
can never overwrite the state a resync is meant to put back.

Every unit gets a row from the start: on first run, any unit with nothing on
file is seeded with the configured default — **on, Cool, fan Auto** — so a
resync always has an answer to return to. Seeding only fills gaps and never
overwrites settings that were observed or set deliberately, and a unit with
nothing on file falls back to those defaults rather than to its own suspect
copy. Setpoint is not part of the default: it is a per-room preference, so
whatever the unit already has is kept. Change the defaults with
`WINDMILL_DEFAULT_MODE`, `WINDMILL_DEFAULT_FAN` and `WINDMILL_DEFAULT_POWER`,
which accept either a name (`cool`, `auto`, `on`) or the raw code.

The restore is verified: settings are written, read back, and re-sent if they
did not stick, and the result reports exactly what was restored. Power is
written last and in a `finally`, so an error partway through can never leave an
AC switched off with nobody told. A restore that fails does not become the new
stored truth.

### Running when nothing asked it to

A separate fault from losing sync, and one you don't notice because the room
feels fine: a unit runs its compressor hard while its room is *already colder*
than its setpoint. `overrun.py` judges every unit over the last hour on two
tests, and the watchdog resyncs the ones it flags (**Auto-fix overrun**, a
separate switch from **Auto-resync** because this verdict is an inference
rather than a reported fault).

**Overcooling** — of the time a unit spent running, how much of it was spent
more than 2 °F below its own setpoint. This test stands alone, which matters:
an entire floor can misbehave at once, and peer comparison sees nothing wrong
when every peer is equally wrong.

**Peer duty** — how hard a unit ran compared to the others on its floor over
the same window. Catches a unit working much harder than its neighbours for
conditions they all share.

The naive version of this test — "cooling while at setpoint" — fires on
everything, because a room sitting at its setpoint is exactly what a working AC
produces. The margin below setpoint is what separates the two, and the split in
this system's own history is not subtle: healthy units spend **0–1%** of their
running time 2 °F or more below setpoint, misbehaving ones **46–58%**. The
threshold sits in the gap.

**What a unit reports right after a resync is not what anyone asked for.**
For several minutes after a software power cycle these units report a transient
state — off, Eco, fan Low — while still connected, so the link check does not
filter it out. Recording that as the desired state made it what the *next*
resync restored, and units converged on switching themselves off: Study
shows mode flipping to Eco at 16:41, 17:21 and 17:55, each one a resync, each
followed minutes later by a return to Cool. Observations are now ignored for six
minutes after a resync starts, measured against the ~4 minutes these units
actually took to settle. A deliberate change through the dashboard ends the
window early, since someone has just said what they want.

**Settings are re-sent once the unit is back on.** A unit that is off may simply
drop writes aimed at it, which is how a resync could restore the power state and
nothing else. They go out before the power-on and again after it — three extra
requests, and the question disappears.

All of that lives in one place. `blynk.apply_settings` is the only thing that
writes settings to a unit, and the resync, the thermostat guard's switch-on and
the watchdog's drift correction all call it. They did not start that way: each
grew its own copy, and the dropped-write bug had to be found and fixed three
separate times — once when a resync left a unit off, once when the guard brought
units back on Eco/Low, and once in the correction pass meant to catch both. The
order that works is `settings → power → settings again`, and there is now
exactly one implementation of it.

**A resynced unit is judged only on what it has done since.** Otherwise the
hour-long window still contains the misbehaviour that prompted the resync, and
the watchdog would grade its own fix on evidence predating it and cycle the unit
again for nothing. This is what makes a short cooldown safe; what actually paces
retries is `MIN_RUN_S` — a unit must accumulate real running time *after* a fix
before it can be judged at all.

Guards, since this power-cycles real hardware on an inference: a cooldown per
unit (30 min by default, settable on the Watchdog tab between 10 min and 6 h),
a limit of two attempts before it gives up and says so (if two cycles haven't
stopped it, a resync isn't the fix), units that are barely running are not
judged, and a unit with no session is never cycled since nothing can reach it.

Attempt counters live in `sync_events`, not in the watchdog's memory, so
restarting the process — which auto-reload does on every code change — cannot
reset a give-up counter and let a unit be cycled indefinitely. A unit returning
to normal logs a `recovered` event, which resets its count so a later relapse
gets a fresh set of tries.

Worth knowing: in this system the units that overcool are **exactly** the units
that accumulate protocol failures (`v31`) — the desync and the pointless
running appear to be the same fault showing up two ways.

### One unit's full history

The **i** button in the corner of each card opens that unit on its own. The top
of the panel is live — read from the unit on open and refreshed every 30 s, not
served from the database — showing room and setpoint, what it is running, draw
and the unit's own 15-minute energy figure, sync state, signal and failure
count, filter status and hours, floor and saved settings.

Below that, **everything it reports**: all 24 datastreams with their live
values. Twelve are identified by name; the other twelve are undocumented and
shown raw rather than hidden, because they are still what the unit is sending.

Then the history, over 6 h / 24 h / 7 days / 30 days: watts, room temperature,
setpoint, WiFi signal and every sync failure, on a shared time axis.

Four stacked panels rather than one chart with four scales — watts, degrees,
dBm and a failure count share no units, and putting them on common axes would
make them look comparable without being so. Stretches with no cloud session are
shaded, so a flat line there is not mistaken for something the unit reported.

Failures are drawn as the *increments*, not the raw counter. `v31` is cumulative
and monotonic, so plotting it directly gives a line that only ever climbs and
says nothing about *when* the unit was struggling — which is the only thing
worth knowing from it.

Everything is drawn as steps, not slopes: readings are written on change and
held until the next one, so interpolating between two samples would draw a
gradual drift the unit never reported.

When a range is longer than the local history, the axis starts where the data
does and says so, rather than leaving three-quarters of the chart empty and
implying the range was covered.

### Backups

Windmill retains 30 days. Everything older exists only in `windmill.db` and
cannot be re-fetched at any price, so it is snapshotted daily with
`VACUUM INTO` -- consistent while the app is writing, which a file copy is not.

Each snapshot is written under a temporary name and only promoted to its dated
one **after** being reopened read-only and checked: integrity, expected tables,
plausible row count. A bad snapshot can therefore never overwrite a good one.
Rotation keeps the most recent `BACKUP_KEEP` and matches a strict filename
pattern, so it can only ever delete its own files.

### Air filters

The **Watchdog** tab reports each unit's filter, and the unit is the authority:
`v11` is Windmill's own **Filter Change Indicator**, 0 while the filter is fine
and 1 once it wants changing. That binding was confirmed by pulling
`dataStreamId=12` from the export API — whose CSV header names it "Filter
Change Indicator" — and matching its values against the pin. It had been an
unbound entry in the datastream map until then.

So there is no threshold invented here. Alongside it, `v7` (Filter Runtime,
cumulative hours since the filter was last reset) is what makes "due"
actionable — 851 hours is a different conversation from 190.

Windmill does not publish the runtime at which a filter is called dirty, and
this refuses to guess one. It reports the bracket its own units imply, and
*learns* the real figure the first time it watches an indicator flip on. An
early "getting close" warning only ever appears once such a flip has been
observed, so it never rests on a made-up number.

A filter change is taken from the indicator going 1 → 0, not from the runtime
counter dropping: `v7` has been seen to step backwards by tens of hours
(670 → 621, 952 → 849) without the filter being touched, so the counter alone
would report changes that never happened.

A newly dirty filter is announced once, on the transition, and lands in the
audit trail. A filter stays dirty until somebody physically changes it, so
repeating the notice every couple of minutes would only train you to ignore it.

### Choosing the integration rule by measurement

`v16` is ground truth, so the integration rule was tested rather than assumed --
see `calibrate.py`, which grades ten candidate rules against the meters.

Two things were wrong. The trapezoid rule contradicted the data model: readings
are written on change and held forward, so the area between two samples is a
rectangle, and the ramp trapezoid drew never happened. The error grew with gap
length, which is why idle units read high. Separately, `integrate_points`
*discarded* an interval longer than `MAX_GAP` where `rebuild_hours` clamped it,
so a keyframe landing one poll late -- 930 s against a 900 s limit, near enough
a coin flip -- contributed zero. That is the opposite-direction error that made
busy units read low.

Both fixed, in both code paths, and the rollup rebuilt. Worst per-unit error
against the meters went from **30.0% to 5.2%**, and the fleet figure moved 0.4%.
`/api/calibrate` now reports *no change warranted* -- the remaining candidates
are within noise of each other.

It also settles a question worth not guessing at: **30 s polling is not the
constraint.** Error is flat from 30 s to 120 s and only reaches 1.45% at 600 s,
so polling faster would buy nothing.

### Against the units' own meters

The dashboard integrates sampled watts; `v16` is each unit's own kWh figure for
the last quarter hour. They are independent measurements of the same thing, and
the **Energy** tab compares them. Measured here and stable across windows:
Kitchen, Living Room and Study agree within 4%, Bedroom reads 13% low,
and Third Floor L/R and Spare Room read 15–23% high. The error is worst on
lightly-used units, which is the expected shape — fewer, shorter compressor
bursts mean more of the total rides on what happened between samples.

**Nothing is applied.** A single fleet-wide factor comes out near 1 (0.94–0.97)
because the per-unit errors point in both directions and cancel, so correcting
by it would nudge the totals a few percent and leave every individual figure
exactly as wrong as it was. The per-unit comparison is the useful output.

The comparison narrows its window to the span the meter actually covers, and
measures both sides across that. The meter's history is younger than the watt
history for most units, and comparing our figure for a whole day against a meter
total covering nine hours makes the integration look wildly wrong — a fact about
the window, not the arithmetic.

### Revision split

`v113` is undocumented, constant per unit, and the only thing found that
predicts which units misbehave: every unit reporting **3** accumulates protocol
failures and overcools, every unit reporting **0** does neither, and the one
unit that does not report it at all is also healthy. Signal strength does not
predict it. It is shown on each card and summarised on the **Watchdog** tab,
because it means the fault tracks a fixed property of the hardware rather than
the room or the usage — which is why a resync never cures it.

### Alerts

The watchdog fixes what a resync can reach. Three things need a person, and
those are the only three that alert: a unit that has **dropped its session**
(only pulling its power helps), a **filter due**, and a unit **still overrunning**
after the attempt limit. An alert that fires for something already being handled
automatically is one you learn to ignore.

Delivery is whatever `.env` configures — a webhook (ntfy, Slack, Discord), email
via SMTP, or a macOS desktop notification, all standard library. Each problem is
announced once, not once per check, and the state lives in the database so a
restart cannot turn a standing problem into a fresh alert every few minutes. A
problem that clears re-arms, so it alerts again if it comes back.

### Schedules

Time-of-day rules on the **Schedule** tab: set a field to a value, on a unit, a
floor, or everything, at a local time on chosen days.

**Rules fire, they do not hold.** A rule applies once when its time passes and
then leaves the unit alone, so a change you make by hand sticks until the next
rule. Continuously enforcing a schedule means the dashboard silently undoing
what someone just did, which is the behaviour people hate in thermostats.

**A missed rule is skipped, not caught up.** If the process is down at 07:00 and
starts at 11:00, applying the 07:00 rule then would act on an intention that
expired hours ago. Only rules whose time passed within the last few minutes run.

Rules can be edited as well as added. **Edit** loads a rule back into the same
form that creates one, so there is a single set of controls with a single set of
rules about what each field accepts, rather than a second inline editor that
could drift from it. Only the fields you actually change are written, and the
resulting field/value pair is validated together — so changing just the time
cannot leave a rule whose value was never checked, and switching a `target 70`
rule to `power` is refused rather than saved as nonsense. An edited rule clears
its last-run stamp, so it can still fire today if its new time is ahead.

Values are validated exactly as `/api/control` validates them, before any unit
is touched.

### Identifying an unbound datastream

Night Mode and Beeping Mode are named by the export API but sit at 0 on every
unit here, so correlating exported values against live pins cannot bind them.
The **API** tab can bind them the other way round: start watching, toggle the
feature in the Windmill app, and whichever pin moves is the one. It reports
every pin that moved, so a coincidence shows up as an ambiguity rather than a
confident wrong answer, and it never writes to an unidentified pin — a wrong
guess there is not a bad reading but a physical effect nobody asked for.

### Filter history

`/api/filter-history` imports the Filter Change Indicator and Filter Runtime
streams from Windmill's export API into the readings table, under their live
pins. That is the only way to see filter changes from before this tool was
logging, and the only way to learn the runtime at which the indicator actually
trips — which is currently known only to lie between 187 h and 629 h. It costs
two exports per unit against the 72/day quota; the hourly maintainer retries
until it lands. Quota refusals are told apart from real errors, since a 400
means both and only one is worth retrying.

### The month ahead

The **Analysis** tab projects where the current month lands. Month-to-date comes
from the `hourly` rollup; the remainder is projected from this system's own kWh
per cooling degree-day rather than a flat daily average, since cooling load
tracks outdoor temperature. When there is not enough weather signal to fit
anything, it falls back to a daily mean and says so.

It reports a range, not just a number, and states what the projection rests on:
how many days the fit has behind it, whether it is extrapolating outside the
temperatures it was built on, and what share of the month-to-date figure was
reconstructed by gapfill rather than measured. When a month is too young to
project it refuses with a reason instead of guessing.

Per-unit projections are an allocation of the household remainder by each unit's
recent share, not seven separate fits -- a unit that starts misbehaving tomorrow
will not show until it moves the total.

### Thermostat guard

Three units have a firmware fault: their own thermostat keeps the compressor
running while the room is already well below its setpoint. Measured waste on the
strict test -- cooling while 2 F or more under target -- is 5.01 kWh/day for
Living Room, 2.85 for Study and 2.04 for Bedroom, against ~0 for the
healthy units. A resync does not cure it and never will; the fault tracks the
hardware.

So the dashboard does the job the unit is not: switch it off below setpoint,
switch it back on when the room returns. `guard.py` decides, `serve.py` acts.

Both the master switch and each unit are **opt-in**, because this drives real
hardware and only some units need it. The guard:

- switches off at `MARGIN_F` below setpoint and back on at `ON_MARGIN_F` above,
  so the two thresholds can never coincide and oscillate -- a module-level
  assert enforces that;
- holds a five-minute minimum in each direction, so a compressor is never
  short-cycled;
- refuses to act on a unit with no session, stale readings, or one that is
  mid-resync, and says which;
- **stands down if you switch a held unit back on yourself.** Whoever did that
  outranks the guard; a dashboard that silently undoes what you just did is the
  behaviour people hate in thermostats;
- is excluded from saved settings while holding a unit off, so "off" never
  becomes the state a resync restores.

Every decision carries a stable reason code and a sentence, for the acting and
the not-acting case alike: "nothing happened" is only trustworthy with the
reason attached.

### Audit trail

Anything that changes something is recorded once and shown twice: `serve.py`
prints it to the terminal as it happens, and the **Audit tab** shows the same
lines later, filterable by range, by who did it, and by unit.

```
18:07:01  INFO  you       Bedroom             set target 86°F  asked for 99, clamped
18:07:01  WARN  you       Bedroom             set mode 9       mode must be one of [0, 1, 2]
18:07:01  INFO  you       floor 2             set fan Low      2 of 2 units
18:07:04  ERR   sampler   —                   poll failed      HTTPError: 502
18:07:04  WARN  watchdog  Third Floor Left    filter needs changing
```

Fixed columns — time, level, who, what it happened to, what happened — with
detail appended dimmed, so a day of output stays scannable. Colour only when
stdout is a terminal; piped to a file or read by launchd, escape codes would be
noise. Everything the process reports goes through the same `log()`, so sampler
errors and reload notices line up with actions instead of each having their own
shape.

The same actions appear in the **browser console**, one collapsed group each:

```
▸ WINDMILL ✓ set target 86°F  Bedroom  41ms
             asked for 99, clamped
    request   /api/control?unit=Bedroom&field=target&value=99
    response  {ok: true, value: 86, …}
```

The wording is the server's own: the description it logs rides back in the
response as `_action`, so the console and the terminal describe an event
identically rather than each parsing the URL and slowly drifting apart. Polling
reads carry no `_action` and are not narrated in either place.

Both outputs come from `record_action`, so the terminal and the tab can never
disagree. The description is built from the request and its response together:
it reports the value that was *applied* rather than the one asked for (setpoints
are clamped, and logging the request would claim something untrue), and marks
anything that failed.

Reads are deliberately not logged. The page polls `/api/summary` and `/api/sync`
every 30 seconds, so logging those would bury the things you actually did under
the dashboard's own background noise.

This is separate from `sync_events`, which is the watchdog's reasoning — every
check that found something, whether or not it acted. The audit trail is the
record of things actually done, by anyone, so "what happened to this unit and
who did it" is one query rather than a reconstruction.

### Floors

Each unit can be assigned a floor from its card. Units are then grouped by
floor, and each floor gets its own setpoint, power and mode controls that apply
to every unit on it at once, plus **Resync floor**. **Sync all** in the toolbar
resyncs everything.

Floor-wide writes validate the value once and then fan out in parallel, one
independent write per unit — a unit that fails or times out does not hold up or
fail the others, and the result says which units took it. A floor's setpoint
reads `mixed` when its units disagree, rather than picking one of them to step
from.

**The watchdog** (a thread in `serve.py`, toggled by *Auto-resync* in the
toolbar) judges every unit every two minutes and resyncs the ones it can reach.
It acts only on **Not syncing**, waits an hour between attempts on the same
unit, and gives up after three — a false positive bounces real hardware, so it
is deliberately reluctant.

The **Watchdog tab** shows what it currently makes of every unit — the verdict
and the evidence behind it, duty cycle against the floor, fixes applied in the
last 24 h — and, when a unit is flagged but nothing is happening, says why it
is holding off and when it will next try. A flagged unit with no explanation
for the inaction reads as a broken dashboard, so the cooldown is stated rather
than implied. Below it, every check that found something and every action
taken, from `sync_events`: automatic fixes, manual ones, and the settings
snapshot captured before each. Nothing it does happens silently. The log filters
by time range using the same presets as the charts, plus **All**, and by unit.
When a range holds more rows than are shipped, the count says so rather than
letting a truncated view look complete.

The overrun check runs every 30 seconds, so a unit is acted on about as soon as
the page could show the problem. It used to be capped at the loop's 120-second
sleep no matter what its own interval said; the loop now ticks faster than
either check and each runs on its own schedule. The verdict costs about a
millisecond to compute from the database, so the pacing that matters is the
cooldown and the attempt limit, not the polling rate.

**Detecting "quiet" is the subtle part.** A setpoint read back proves nothing:
writes are echoed by the cloud whether or not the hardware saw them. Only pins
the *unit* pushes are evidence, and of those only **RSSI (`v30`)** moves
independently of what the AC is doing. Room temperature and watts go flat for
hours on a unit that is merely idle — measured against this system's own
history, the longest gap between changes on a *healthy* unit was 37 h for room
temperature and about 3 h for watts, against ~25 min for RSSI. So RSSI is the
heartbeat, and the threshold sits at an hour to stay well clear of it.

`v31` (Protocol Failure Count) is worth watching alongside it: it climbs a few
times an hour on units that struggle to stay in sync and stays flat on ones that
don't, and it does not track signal strength — the unit here with the strongest
RSSI also has the highest failure rate.

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
| 8 | Night Mode | unbound |
| 11 | Beeping Mode | unbound |
| 12 | Filter Change Indicator | `v11` |
| — | *Cloud session* (synthetic, added by this tool) | `c0` |

Every name above was read from the header of that stream's own CSV export, so
they are Windmill's names rather than guesses. Only these report history; the
rest of the pins are live-only and shown raw in the **All metrics** table.

Ids 4, 5, 6, 10, 13 and everything above 15 are **unverified** — the export
quota ran out mid-survey, and a quota refusal looks exactly like a missing
stream. An earlier version of this table claimed 16/17/18 were Mode, Fan Speed
and Energy; that is unconfirmed and those three appear as `v3`, `v4` and `v16`
on the live pins regardless.

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

**`v16` is "Energy" — kWh consumed in the trailing 15 minutes.** It reports every
15 minutes and is per-interval, not cumulative, which is why it is sometimes seen
*decreasing*. Confirmed by integrating `v15` over the preceding 15 minutes and
comparing: the ratio holds at 0.98–1.01 across every window checked. This project
still integrates `v15` itself, since that works at any resolution and does not
depend on the unit's reporting cadence.

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
