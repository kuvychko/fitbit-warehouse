## Context

The `health` schema holds ~10 years of intraday streams (heart rate at 5–15 s,
per-minute steps/calories/AZM) plus daily/nightly summary tables. The single
provisioned dashboard (health-overview) reads raw hypertables at a 90-day
window; multi-year interactive panels over intraday data are infeasible without
pre-aggregation. The companion homelab change swaps the shared `warehouse-db`
to `timescale/timescaledb-ha` (community tag), making `timescaledb_toolkit`
available; this change adopts it in the tenant. Design decisions below were
settled in exploration with the author.

## Goals / Non-Goals

**Goals:**
- Interactive multi-year panels over intraday-derived statistics (percentile
  bands, activity volume) via hourly continuous aggregates.
- Robust statistics: percentiles from composable sketches, never raw min/max.
- One shared baseline definition (trailing 30-day median + p25/p75) reused by
  scoreboard and morning report.
- Timezone flexibility preserved: no cagg encodes a civil-day boundary; the
  `$timezone` dashboard variable keeps working.
- Device-aware aggregation per the existing health-dashboards spec (no naive
  cross-device sums).
- Seam-free "now" edge: real-time aggregates so the 2-hour API poller's newest
  rows appear without waiting for a refresh.

**Non-Goals:**
- No body-fat panels (author has almost no data; anyone with data can extend).
- No real-time/live dashboards — sync cadence is ~2 h; the shortest horizon is
  the morning report, framed as "last night," not "now."
- No recomputed heart-rate zones from raw bpm (age/device-dependent thresholds
  invent false precision) — Fitbit's own AZM is the zone source; the Basis era
  simply has no zone data.
- No backup implementation (per project charter; the image swap's dump/restore
  mechanics live in the homelab change).

## Decisions

### D1. Hourly grain for all caggs (including AZM)
Daily caggs would freeze a day boundary at materialization time, defeating the
`$timezone` variable and the timezone-fidelity work. Hour buckets are
timezone-neutral for whole-hour offsets; dashboards regroup hours into civil
days at query time. Data reduction (~300× for heart rate) is ample.
*Alternative rejected*: daily caggs in home timezone — cheaper queries, but
re-freezes the boundary we just made flexible.

### D2. Toolkit `percentile_agg` sketches, not `percentile_cont` arrays
Percentile numbers don't compose across buckets; uddsketch sketches do.
`heart_rate_hourly` stores `percentile_agg(bpm)` + `count(*)` per
(hour, device, source); queries read `approx_percentile(p, rollup(sketch))`
for any percentile at any zoom in any timezone (~1% relative error,
irrelevant at trend zoom). *Alternative rejected*: plain `percentile_cont`
in finalized caggs (no toolkit needed) — exact per-hour but non-composable;
daily values become "percentile of hourly percentiles," a weaker statistic.
The toolkit dependency was deemed worth it cluster-wide.

### D3. Real-time aggregates, conservative refresh policies
Caggs are created with real-time reads enabled (materialized history + live
tail over unmaterialized chunks), so the newest poll is always visible.
Refresh policies consolidate daily; exact `start_offset` must clear the
90-day compression boundary interplay (refresh window ends before compressed
chunks begin, or relies on TimescaleDB's compressed-cagg support in the
pinned version — verify during implementation).

### D4. Baseline = trailing 30d median, defined once in `daily_baseline`
A view (not a cagg — daily grain over daily tables is cheap) computing per
(day, metric): value, median, p25, p75 over the trailing 30 days via LATERAL
`percentile_cont`. Metrics: resting HR, HRV (rmssd), sleep minutes, sleep
score, breathing rate, nightly temp deviation, daily steps. Morning report
reads the last row; scoreboard reads the last ~30; trends may reuse the bands.
*Note*: `percentile_cont` is not a window function in Postgres — LATERAL is
the pattern.

### D5. Sleep composition from `levels_summary`, no new cagg
`sleep_session.levels_summary` jsonb already carries per-stage minutes per
night; a `sleep_composition` view unpacks it. Handles both vocabularies
(stages: wake/light/deep/rem; classic: awake/restless/asleep).

### D6. Device-era regions derived from `heart_rate_hourly`
Grafana annotation queries (time + timeEnd → background regions) from
`SELECT device, min(hour), max(hour) ... GROUP BY device`. Heart-rate
presence defines wearable eras (phones never record HR). Eras may overlap
slightly during device transitions — acceptable for background tinting.

### D7. Correlation panels are a row inside Trends
Grafana XY-chart panels: trailing-28d avg steps → weigh-in weight;
previous-day activity → that night's sleep score; trailing-28d activity →
RHR. Points colored by year (sequential palette) to expose era drift. Panel
titles state lag direction explicitly; never causal language.

### D8. Image pins aligned with the platform
`db` and `migrate` services pin the same `timescale/timescaledb-ha` community
tag as homelab's `warehouse-db` (target: `pg17.10-ts2.28.2`; both amd64 and
arm64 published). Not the `-oss` variant (drops TSL features: compression,
toolkit); not `-all` (bundles every PG major, needless weight). Migration 004
fails fast with a clear error if `timescaledb_toolkit` is unavailable
(`CREATE EXTENSION IF NOT EXISTS` requires appropriate privilege — in shared
mode the platform pre-installs the extension; standalone bootstrap can create
it).

### D9. Dashboard design language per horizon
- **Trends**: 28d-median lines with p25–p75 bands; year-over-year overlay;
  seasonality (month-of-year across years); device-era regions; correlation
  row. Multi-year default range.
- **Scoreboard**: stat tiles vs 30d baseline (green/red framed against own
  history, not fixed goals), calendar heatmap of steps, streak counters
  (gaps-and-islands SQL), WHO 150 min/week AZM bar, personal bests, 30d range.
- **Morning report**: hypnogram + nighttime HR merged overlay, readiness
  composite (see D11), recovery tiles (HRV/RHR/breathing/SpO2/temp vs
  baseline with deviation arrows), nap chip, today's weigh-in, today's
  activity (live) + yesterday's HR curve/AZM/steps recap, "data as of"
  freshness stat. Auto-zoomed to last night ± 10 min (see D13).
Follow the dataviz skill when authoring panels (percentile bands, sequential
year palette, heatmap scales).

### D10. Primary sleep session inferred uniformly, not from a source flag
**Discovered post-implementation** (2026-07-13, exploring a live score
mismatch): `sleep_session.main_sleep` is Takeout-only. The Google Health API
`sleep` resource carries only `interval`/`type`/`stages` — no `logId`,
`mainSleep`, or `levels_summary` equivalent (confirmed against the payload
shape in `tests/test_poller_mappers.py`; `sync/poller.py:map_sleep` correctly
leaves these `NULL` because the API gives it nothing to map). Every query
that identifies "last night" filtered `WHERE main_sleep`, which silently
excludes every sync-sourced night forever — the morning report has been
stuck on the last Takeout-backfilled night since sync took over, not
actually last night. This also broke `daily_metric`'s `sleep_minutes` and
`sleep_score` rows (both gated on `ss.main_sleep`, `004_analytics.sql:161-168`),
freezing their 30-day baselines the same way.

Fix: a `health.primary_sleep_session` view defining "main sleep" uniformly
as the session with the greatest `minutes_asleep` (falling back to
`duration_ms` when null) per civil `day`, computed the same way regardless
of `source`. Every place that currently does `WHERE main_sleep` (hypnogram,
duration/efficiency stat, `daily_metric.sleep_minutes`, naps) reads this
view instead. Naps fall out for free: any other same-day `sleep_session` row
not selected as primary.
*Alternative rejected*: keep using `main_sleep` for backfill rows and add a
separate inferred-flag path for API rows — two definitions of "main sleep"
that could disagree at the backfill/sync boundary, exactly the kind of seam
this project has otherwise avoided (see the timezone-fidelity and
breathing-rate-day-join precedents already in `004_analytics.sql`).

### D11. No live Sleep Score — a local readiness composite instead
Fitbit's proprietary Sleep Score has no equivalent field anywhere in the
Google Health API sleep payload (same evidence as D10). It is not a mapping
bug; the number the app shows overnight isn't obtainable from the sync
source at all. Rather than leave the morning report perpetually showing a
stale Takeout-only number, replace it with two panels computed from metrics
that *are* live: HRV, RHR, breathing rate, SpO2, temp deviation, sleep
duration, and efficiency, each already available as a `daily_baseline`
deviation.
- **Vibe score**: mean of sign-corrected, IQR-scaled deviations across all
  components — a smoothed single number.
- **Caution flag**: worst single component (or "≥2 components outside
  p25–p75"), so one bad metric (e.g. elevated temp) can't be averaged away
  by an otherwise-good night — an average alone would mask exactly the
  illness-signal case this panel exists for.
Historical Sleep Score (Takeout-backfilled nights only) remains queryable
in Trends/Scoreboard from `health.sleep_score` as-is; this only concerns the
morning report's "how did I do last night" framing.

**Correction during implementation (2026-07-13):** breathing rate and
nightly skin temperature turn out to be Takeout-only too — `sync/poller.py`
has no puller for either (grepped, zero matches), so their
`daily_baseline` rows are just as frozen as `sleep_score`'s, for the same
structural reason. The composite therefore draws from HRV, RHR, and sleep
minutes — the three metrics genuinely live via the API and already present
in `daily_baseline` without further schema changes (SpO2 was tried as a
fourth and reverted for a perf reason — see D14). Three components is
thinner than originally envisioned, but each one is real and won't
silently freeze. Breathing rate/temp recovery tiles are unaffected by this
change (out of this amendment's scope) but will silently go stale the same
way `main_sleep` did; worth its own follow-up, not fixed here.

### D12/D13 superseded — see D15
The relative time override chosen in D13 (`now-14h` to `now-1h`) turned out
to drift exactly as predicted ("won't always perfectly frame an unusual
night" undersold it — it drifted on *ordinary* nights too, depending purely
on what time of day the dashboard was viewed). The same relative-window
approach on "Yesterday's heart rate" (D-14-adjacent, not its own lettered
decision at the time) had the identical problem. D15 replaces both. Kept
below for the historical record of what was tried and why it didn't hold up
in practice, not as the current design.

### D12 (historical). Hypnogram merged with nighttime HR via per-stage region annotations
Grafana natively renders annotation queries (`time`, `timeEnd`, `tags`) as
colored background regions on a timeseries panel — no custom plugin needed.
One annotation query per sleep stage (`WHERE level = 'deep'/'light'/'rem'/
'wake'`), each assigned a fixed color, layered behind the nighttime HR line
in a single panel — the Fitbit-app-style combined view, replacing the
separate state-timeline + HR panels.
*Alternative rejected*: stack a state-timeline panel directly above/below
the HR timeseries with matched x-axis to fake a merge — visually close but
still two panels with two independent legends/tooltips; true annotation
regions are a real overlay in one panel.

### D13 (historical). No true auto-zoom — a fixed, generous relative window instead
Spiked live against the running Grafana 11.4 instance: saving a dashboard
with `time.from`/`time.to` set to `${variable}` strings is accepted without
validation (no special-casing), and Grafana's time range is resolved via
`dateMath` before template variables are hydrated — `${var}` is never
interpolated there. Confirmed dead end, not just a hunch.

The originally-planned fallback (numeric "minutes since sleep onset" x-axis
via an `xychart` panel, guaranteed to auto-fit) turned out to conflict with
D12: Grafana's native region annotations — the mechanism D12 depends on for
the colored stage bands — only attach to a time-domain axis. `xychart`
panels don't support them. Auto-zoom and the colored overlay can't both be
had through Grafana's built-in mechanisms simultaneously.

Resolved with the author: keep the colored overlay (time-domain panel,
native annotations), drop precise ±10 min auto-zoom. The merged panel gets
a **panel-level time override** (`timeFrom: "13h"`, `timeShift: "1h"` →
effectively `now-14h` to `now-1h`) — fixed relative to "now" at render
time, not day-anchored, so it does drift as the day goes on and won't
always perfectly frame an unusual night. Comfortably covers the observed
real sleep windows (~21:30–05:30 local) on any normal morning viewing.
Scoped as a panel-level override, not a dashboard-level time range change,
so it doesn't affect other panels (e.g. "yesterday's heart rate," which
still wants the dashboard's existing wider default). The user can always
widen the panel's time range manually in Grafana.
*Rejected*: a script that rewrites the dashboard JSON's absolute time range
each morning (the existing 30s file-provisioning auto-reload would pick it
up) — would give true precision with both mechanisms intact, but is a new
scheduled component not currently in scope; worth reconsidering later if
the fixed window proves too imprecise in practice.

### D14. Today's activity and weigh-in as raw dashboard queries; SpO2 promoted to `daily_metric`
No new schema objects for activity/weight: `steps_hourly`/`azm_hourly`/
`calories_hourly` (D1, already real-time) cover today's live cumulative
activity stat the same way the existing "yesterday's activity" panel reads
them; today's weigh-in reads `health.weight` directly.

**Tried and reverted during implementation:** promoting SpO2 into
`daily_metric` (avg `pct` over each night's `primary_sleep_session` window)
was tempting — live data confirms ~400-500 overnight samples/night via the
API poller, genuinely live unlike breathing rate/temp — but it hit the
exact perf blowup this file already has a scar for: `daily_metric` is a
plain (non-materialized) view, so `daily_baseline`'s LATERAL correlated
subquery re-evaluates every UNION ALL branch per (day, metric) row, and a
raw join+aggregate against the `spo2` hypertable inside that branch does
not survive being re-run that many times. Confirmed live: cancelled after
>2 minutes still running against ~2 weeks of data. This is the same failure
mode already reverted once for breathing_rate's day-join (git history:
"Revert breathing_rate LATERAL join in daily_metric (perf regression)").
SpO2 stays a raw per-panel query as originally planned — no 30-day baseline
for it, just last night's avg/min shown directly. The readiness composite
(D11) accordingly drops back to HRV, RHR, and sleep minutes only — three
components, not four. A materialized/cagg-backed SpO2 daily summary could
revisit this later, but that's real new scope, not a fit for this
amendment.

### D15. Sleep/HR panels anchored to `primary_sleep_session`, not to Grafana's time axis at all
**Amended 2026-07-15**, after D13's relative-window compromise turned out
to drift in practice, not just in edge cases — both the hypnogram panel
and "Yesterday's heart rate" (relying on the dashboard's default `now-48h`
to `now`) rendered a different, sometimes-wrong slice of data depending
purely on what time of day the report was viewed. Root cause was always
architectural: these panels' real anchor is a *fixed event* (bedtime/wake
time), but every fix attempted so far (variable-interpolated time range,
a relative `timeFrom`/`timeShift` override, a wider dashboard-default
window) still routed through Grafana's "now"-relative time-range
machinery, which structurally cannot express "locked to an absolute event
already in the past" — only "N hours before whenever this happens to be
evaluated."

Resolved by removing Grafana's time axis from the picture entirely for
these three panels. Each becomes an `xychart` panel with a *numeric* X
axis computed directly in SQL as hours relative to `primary_sleep_session`
(source-agnostic "main sleep," per D10 — naps are structurally excluded
already, satisfying "auto-detect the major sleep chunk, ignore naps" with
no new logic):
- **"Last night's sleep"**: X = hours since `start_time` (small negative
  padding before, positive through wake). Never drifts — it isn't
  expressed relative to "now" at any point, only relative to the sleep
  session's own start, which is a fixed instant once that session exists.
- **"Yesterday's heart rate"**: X = hours before `start_time`, spanning
  the 24h leading up to bedtime — a "day before sleep" framing, not a UTC/
  civil-day "yesterday". Same drift-immunity.
- **New "Today's heart rate"**: X = hours since `end_time` (wake time), no
  upper bound — naturally grows through the day as new samples sync in,
  stacked directly under "Yesterday's" for day-over-day comparison (the
  live-updating counterpart the author asked for alongside this fix).
- **New "Sleep timing" stat panel**: a plain readout of `start_time`/
  `end_time`, added because an hour-offset X axis is meaningless without
  stating what hour 0 actually is in wall-clock terms — a numeric axis
  trades away Grafana's usual free wall-clock labeling, so something has
  to supply that context back.

**Cost paid**: D12's colored background bands (native Grafana region
annotations) are gone — `xychart` panels have no time-domain axis for
annotations to attach to, so that whole mechanism is moot, not just
inconvenient. Stage coloring on the sleep panel is per-point instead, each
2-minute HR sample tagged with whichever `sleep_stage` segment contains it.
Dense enough (~250 points over a night) to read as a continuous colored
trace. Server-auto-assigned categorical colors, not the specific purple/
blue/green/amber palette D12 hand-picked — no evidence found that xychart
supports an explicit value→color map the way the old state-timeline
mappings did.

**First deploy of this design was actually broken** — copied the
`"x": "fieldname"`, `"pointColor": {"field": "fieldname"}` shape from
Trends' existing correlation-row xychart panels (assumed working prior
art) without verifying it against *this* Grafana version. Symptom once
live: uniform single-color points, no line connecting them, no visible
stage variety. Root-caused by reading the xychart plugin's actual
TypeScript source inside the running container
(`/usr/share/grafana/public/app/plugins/panel/xychart/panelcfg.gen.ts` vs
`panelcfgold.gen.ts`) rather than guessing further: Grafana 13's xychart
uses a *matcher*-based options schema —
`"options": {"mapping": "manual", "series": [{"x": {"matcher": {"id":
"byName", "options": "fieldname"}}, "y": {...}, "color": {...}}]}` — and
`fieldConfig.defaults.custom.show` defaults to `"points"` only, never
`"points+lines"`, unless set explicitly. The plain-string shape used
throughout (here and in Trends) is from an older schema
(`panelcfgold.gen.ts`) that a migration handler *can* upgrade automatically
— but only when `panel.pluginVersion` is unset or `< 11.1`, and evidently
didn't trigger cleanly here. Trends' existing xychart panels use the same
old-style shape and have never been visually confirmed either — worth
checking next time that dashboard is actually opened in a browser, not
assumed correct because it shipped first.

**Rejected**: the scheduled-script alternative D13 flagged as worth
reconsidering (rewrite the dashboard JSON's absolute time bounds each
morning, letting the 30s file-provisioning watcher pick it up) — would
have kept the native background bands, but at the cost of a new
operational component (something to schedule, something to keep in sync
with git-based deployment of the same file, something that can fail
silently). The numeric-axis approach needed no new infrastructure at all
and directly eliminates the failure mode rather than working around it,
which is why it won over rewriting time bounds precisely.

**Left inconsistent, not fixed here**: "Yesterday's activity" (steps/AZM,
a stat tile) still means calendar-day yesterday, not bedtime-anchored like
the HR panel now sitting next to it in the same row. It's not subject to
the axis-drift bug (stat tiles don't have an axis), so it wasn't in scope
for this fix, but the two panels now describe "yesterday" differently.
Worth reconciling later.

## Risks / Trade-offs

- [Toolkit missing in shared mode at migrate time] → 004 checks
  `pg_available_extensions`/`CREATE EXTENSION` early with a clear failure
  message; sequencing documented: homelab change lands first.
- [Sketch columns enter dumps; restores need toolkit at compatible version]
  → documented in README backup note; the homelab change re-runs its restore
  drill; this repo only documents the requirement (charter).
- [Sparse worn-hours skew: hours with few samples] → `samples` column in
  every cagg; queries can filter (e.g., `samples >= 30`) when composing
  daily statistics.
- [Cagg refresh vs 90-day compression policy interplay] → verify supported
  behavior for the pinned TimescaleDB version during implementation; adjust
  refresh `start_offset` if needed.
- [Half-hour timezone offsets blur hourly buckets] → accepted; documented
  limitation (author's zones are whole-hour; strangers in :30 zones see
  ±30 min boundary blur at daily grain, invisible at trend zoom).
- [Grafana panel types (XY chart, state timeline, heatmap) vary by version]
  → standalone pin is Grafana 11.4 (has all three); shared-mode host Grafana
  must be ≥10 for XY charts — noted in docs.
- [BREAKING: standalone volumes can't cross musl→glibc] → called out in
  proposal/README; project is days old, no known external users; fresh init
  + idempotent migrations/backfill is the documented path.
- [`main_sleep`/`log_id`/`levels_summary` are Takeout-only; sync can never
  populate them] → D10's `primary_sleep_session` view removes the
  dependency on the flag entirely rather than trying to synthesize the
  missing fields. Anything still joining `sleep_score` by `log_id` (baseline
  view) will simply never match post-backfill nights — expected, per D11,
  not a bug to chase further.
- [Reopening 004_analytics.sql's `daily_metric` view after it has already
  run in production] → `CREATE OR REPLACE VIEW` makes this a safe re-apply
  (unlike the `IF NOT EXISTS` table/cagg statements in the same file); no
  new migration number needed for the `sleep_minutes`/`sleep_score` join
  fix or the new `primary_sleep_session` view. Confirm this reasoning holds
  before implementation — if 004 is amended in place, the tasks.md
  correction below should say so explicitly so a future reader doesn't
  assume 004 was untouched since the original run.

## Migration Plan

1. Homelab change lands: `warehouse-db` on `timescaledb-ha`, toolkit extension
   created in the `warehouse` database, tenants restored, restore drill passes.
2. This change: bump image pins; add `004_analytics.sql`; run migrate (shared
   mode: against the upgraded cluster; standalone: fresh volume).
3. Provision the three dashboards; verify against real data ranges (author),
   and against synthesized data for docs/screenshots.
4. Rollback: drop caggs/views (004 objects are additive; raw tables untouched);
   revert image pins (standalone rollback also needs a volume rebuild).

## Open Questions

- Exact refresh policy offsets per cagg (resolve during implementation against
  the pinned version's compressed-cagg semantics).
- Whether `azm_hourly` needs a `zone` dimension in its primary grouping or a
  pivoted layout — decide when writing the WHO scoreboard query.
- (D13) Does this Grafana version/schemaVersion honor a variable-interpolated
  `time.from`/`time.to` on a provisioned dashboard? Spike before committing;
  fall back to the minutes-since-onset numeric x-axis if not.
- (D10) Amend `004_analytics.sql` in place vs. a new migration file for
  `primary_sleep_session` and the `daily_metric` join fix — leaning toward
  amending in place (views are `CREATE OR REPLACE`, safe to re-apply) but
  confirm no downstream consumer depends on the current (broken) behavior
  before doing so.
