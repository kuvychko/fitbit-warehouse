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
- **Morning report**: hypnogram (sleep_stage state timeline), recovery tiles
  (HRV/RHR/breathing/temp vs baseline with deviation arrows), yesterday's HR
  curve + AZM + steps, "data as of" freshness stat. `now-24h` range.
Follow the dataviz skill when authoring panels (percentile bands, sequential
year palette, heatmap scales).

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
