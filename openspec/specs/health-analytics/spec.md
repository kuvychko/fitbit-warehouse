# health-analytics

## Purpose

The SQL analytics layer over the raw intraday hypertables: hourly continuous
aggregates (percentile sketches for heart rate; sums for steps, calories,
AZM) that let multi-year dashboards render interactively without scanning
raw chunks, plus shared views (`daily_baseline`, `primary_sleep_session`,
`sleep_composition`) that give every dashboard one consistent definition of
"baseline" and "last night's sleep." Requires the `timescaledb_toolkit`
extension. Implemented in `infra/migrations/004_analytics.sql`, owned by
`health_owner`, readable by `health_ro`.

## Requirements

### Requirement: Toolkit availability in both deployment modes
The analytics migration SHALL require the `timescaledb_toolkit` extension and
fail fast with a clear error when it is unavailable. The standalone compose
profile SHALL pin `db` and `migrate` to a `timescale/timescaledb-ha` community
tag (never `-oss`) so toolkit is present without homelab context; shared mode
SHALL document that the platform pre-installs the extension.

#### Scenario: Toolkit missing
- **WHEN** migration 004 runs against a database without `timescaledb_toolkit`
- **THEN** it aborts before creating any object, with an error naming the
  extension and pointing at the image/platform requirement

#### Scenario: Standalone stranger setup
- **WHEN** a new user runs the standalone profile and migrations on a fresh
  volume
- **THEN** migration 004 completes without any manual extension installation

### Requirement: Hourly continuous aggregates over intraday streams
The schema SHALL provide continuous aggregates at one-hour grain — never
daily — over `heart_rate` (percentile sketch via `percentile_agg(bpm)` plus
sample count), and `steps`, `calories`, `azm` (sums; AZM per zone), each
grouped by device and source. Hour buckets keep civil-day boundaries a
query-time concern so the dashboards' timezone variable keeps working.

#### Scenario: True daily percentiles in any timezone
- **WHEN** a query rolls up `heart_rate_hourly` sketches into civil days for an
  arbitrary timezone via `rollup()` + `approx_percentile()`
- **THEN** it returns daily percentiles consistent (within sketch error) with
  computing them from the raw table, without scanning raw chunks

#### Scenario: Sparse hour discounting
- **WHEN** a device was worn only minutes within an hour
- **THEN** the cagg row exposes a sample count that queries can filter on

### Requirement: Real-time aggregate reads
The continuous aggregates SHALL serve real-time reads (materialized history
plus live tail), so rows written by the 2-hourly API poller are visible in
dashboard queries immediately, and SHALL have refresh policies that
consolidate recent buckets on at most a daily cadence without conflicting
with the 90-day compression policies on the source hypertables.

#### Scenario: Fresh poll visible before refresh
- **WHEN** the poller inserts intraday rows and no cagg refresh has run since
- **THEN** a query against the cagg already includes those rows

### Requirement: Shared 30-day baseline view
The schema SHALL provide a `daily_baseline` view exposing, per day and metric
(resting heart rate, HRV rmssd, minutes asleep, sleep score, breathing rate,
nightly temperature deviation, daily steps): the day's value and the trailing
30-day median, p25, and p75. All dashboards SHALL source baseline comparisons
from this view so the definition never diverges.

#### Scenario: Consistent baseline across dashboards
- **WHEN** the morning report and the scoreboard both show a metric's deviation
  from baseline for the same day
- **THEN** both derive from the same `daily_baseline` row and agree exactly

### Requirement: Primary sleep session inference
The schema SHALL provide a `health.primary_sleep_session` view identifying,
per civil day, the "main sleep" session as the one with the greatest
`minutes_asleep` (falling back to `duration_ms` when null) among that day's
`sleep_session` rows — computed identically regardless of `source`. Every
query that needs "last night" or "today's main sleep" SHALL use this view
rather than filtering `sleep_session.main_sleep` directly, since that column
is populated only for Takeout-backfilled rows and is always `NULL` for
API-synced rows (the Google Health API sleep payload carries no equivalent
field). `daily_metric`'s `sleep_minutes` row SHALL source from this view.

#### Scenario: Sync-only night resolves correctly
- **WHEN** a night's `sleep_session` row was written by the API poller
  (`source = 'api'`, `main_sleep IS NULL`)
- **THEN** `primary_sleep_session` still identifies it as that day's main
  sleep if it is the longest session recorded for the day

#### Scenario: Secondary same-day sessions are naps
- **WHEN** a civil day has more than one `sleep_session` row
- **THEN** every row other than the one selected by
  `primary_sleep_session` for that day is a nap, queryable without a
  separate table

### Requirement: Sleep composition view
The schema SHALL provide a `sleep_composition` view unpacking per-stage minutes
per night from `sleep_session.levels_summary`, handling both the stages
vocabulary (wake/light/deep/rem) and the classic vocabulary
(awake/restless/asleep), without a new hypertable or cagg.

#### Scenario: Stages-era night
- **WHEN** a night's session has `sleep_type = 'stages'`
- **THEN** the view yields one row per stage with its minutes for that night

### Requirement: Read-only access to analytics objects
All continuous aggregates and views SHALL be owned by `health_owner` and
readable by `health_ro`, so Grafana needs no new role or grant beyond the
existing datasource.

#### Scenario: Grafana reads a cagg
- **WHEN** the provisioned datasource (role `health_ro`) queries any analytics
  object
- **THEN** the query succeeds, and INSERT/UPDATE on the same objects fails
