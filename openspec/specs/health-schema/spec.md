# health-schema

## Purpose

The `health` schema, roles, and hypertable layout — the contract every
writer (Takeout backfill, Google Fit backfill, API poller) and reader
(Grafana) depends on. Live on `warehouse-db` since 2026-07-11 (change
`health-tenant-foundation`); extended with timezone fidelity 2026-07-12
(change `timezone-fidelity`).

## Requirements

### Requirement: Idempotent tenant bootstrap
Migrations SHALL create schema `health` and roles `health_owner`, `health_rw`,
`health_ro` idempotently (guarded so re-runs are no-ops and no database-level
CREATE privilege is assumed), with `search_path = health, public` on each role
and default privileges so `_rw`/`_ro` automatically cover future tables.

#### Scenario: Re-run is a no-op
- **WHEN** all migrations are applied twice against the same database
- **THEN** the second run completes without error and changes nothing

#### Scenario: Least privilege
- **WHEN** bootstrap has run
- **THEN** `health_rw` can `INSERT`/`SELECT`/`UPDATE` in `health`, `health_ro`
  can only `SELECT`, and neither has privileges on other schemas (beyond
  `public` usage)

### Requirement: Metric hypertables with natural keys
The schema SHALL define TimescaleDB hypertables for the core metric families
confirmed present in the exports — intraday heart rate, resting heart rate,
sleep sessions with stage breakdowns and scores, steps,
calories/distance/activity levels, SpO2, HRV, breathing rate, skin
temperature, active-zone minutes, and weight/body fat — each with a unique
constraint on its natural time grain and provenance columns: `source`
(`fitbit-takeout`, `googlefit-takeout`, or `api`) and, where the data carries
it, the recording `device` (e.g. "Charge 5", "Basis Peak"). All time columns
SHALL be `timestamptz` stored as UTC.

#### Scenario: Hypertables exist
- **WHEN** migrations have run
- **THEN** each metric table is a hypertable (`create_hypertable` applied) and
  has a unique constraint on its natural key

#### Scenario: Duplicate writes are absorbed
- **WHEN** the same rows are written twice through the documented upsert path
- **THEN** row counts do not change on the second write

### Requirement: Two-mode deployment
The project SHALL run either standalone (bundled TimescaleDB via a compose
profile, database `warehouse`) or against an external shared cluster
(`PG_HOST`), with all configuration from `.env` and every variable documented
in a committed `.env.example`.

#### Scenario: Standalone from a fresh clone
- **WHEN** a user copies `.env.example` to `.env`, fills placeholders, and
  starts the standalone profile
- **THEN** the DB comes up, migrations apply cleanly, and no committed file
  needed editing

#### Scenario: Shared-cluster mode
- **WHEN** `PG_HOST` points at an external TimescaleDB and the standalone
  profile is not used
- **THEN** migrations and services run against that host and no local DB
  container is created

### Requirement: Per-sample UTC offset retained
The schema SHALL add a nullable `utc_offset_s integer` column to the
list-fed metric tables (`heart_rate`, `spo2`, `hrv`, `azm`, `weight`,
`body_fat`, `sleep_session`, `sleep_stage`) recording the local-time offset
(seconds) the measurement was experienced at, where the source provides it.
NULL means "unknown — assume home zone". The migration SHALL be additive
and safe on compressed hypertables (nullable, no default).

#### Scenario: Additive migration re-runs cleanly
- **WHEN** migration 003 runs against a database with populated, partially
  compressed hypertables, twice
- **THEN** both runs succeed without rewriting rows and the columns exist

### Requirement: Fitbit-civil-day steps table
The schema SHALL define a `steps_daily` hypertable keyed on `day` (a Fitbit
civil date, travel-aware) with `steps`, `source`, and optional `device`
columns, upserted with DO UPDATE (daily totals revise intra-day).

#### Scenario: Day reflects where the user was
- **WHEN** a day's steps were recorded in a non-home timezone
- **THEN** the `steps_daily` row for that civil date carries the total as
  the Fitbit app displayed it locally

### Requirement: Strava activity table and track hypertable
The schema SHALL define two additional tables.

`strava_activity` is a plain (non-hypertable) table with one row per Strava
activity keyed on the immutable Strava activity ID (unlike every other table
in this schema, which keys on a time grain — `strava_activity` is not a
hypertable precisely because its natural key isn't one). It SHALL carry at
least: `activity_id` (the Strava ID, unique key), `start_time timestamptz`
stored as **UTC** (from the export/API's canonical UTC start, never a naive
local time), `utc_offset_s` for civil-day/week grouping, `activity_type`
(the coarse Strava type, e.g. `Run` — the granular `sport_type` is not in the
bulk export, so it is not stored),
`name`, `distance_m`, `moving_time_s`, `elapsed_time_s`, `elev_gain_m`,
`elev_loss_m`, and a `source` provenance column
(`strava-export` | `strava-api`). Both `moving_time_s` and `elapsed_time_s`
SHALL be stored: pace is defined on moving time, the heart-rate correlation
window on elapsed time.

`activity_track` is a hypertable partitioned on `time` with one row per GPS
fix keyed on `(activity_id, time)`. `time` SHALL be derived identically in
every ingestion path as the activity's UTC start plus the point's
integer-second offset (`start_time + round(offset_seconds)`), so backfill
(from GPX) and sync (from the stream offsets) produce identical keys and
overlap dedups instead of duplicating. `activity_track` SHALL carry plain
`lat`, `lon`, and `elevation_m` columns, a `source` provenance column
(excluded from the natural key), and a generated `geography(Point, 4326)`
column with a GiST index. It SHALL be created with an explicit
`chunk_time_interval` (not the 7-day default) and a compression policy,
consistent with the other low-frequency hypertables in this schema.

#### Scenario: Activity table and track hypertable exist
- **WHEN** migrations have run
- **THEN** `strava_activity` has a unique constraint on the Strava activity
  ID and is not a hypertable, `activity_track` is a hypertable with a
  unique constraint on `(activity_id, time)`, and `activity_track` has a
  GiST index on its geography column

#### Scenario: Activity edits propagate
- **WHEN** the same Strava activity ID is upserted twice with different
  summary values (e.g. a renamed activity)
- **THEN** `strava_activity` reflects the latest values, not the first

#### Scenario: Backfill and sync agree on track-point identity
- **WHEN** the same activity's track is loaded once by backfill (from its
  GPX file) and once by sync (from the stream offsets)
- **THEN** both produce the same `(activity_id, time)` keys, so the second
  load adds no duplicate points

### Requirement: Platform-extension guard for PostGIS
The migration SHALL require the `postgis` extension and fail fast with a
clear error when it is unavailable, using the same guard idiom as
`infra/migrations/004_analytics.sql`'s existing `timescaledb_toolkit`
guard: attempt `CREATE EXTENSION IF NOT EXISTS postgis`, swallowing an
`insufficient_privilege` error (the expected outcome for a shared-mode
tenant role with no `CREATE` on the database), then unconditionally verify
the extension is present in `pg_extension` and abort with a clear error if
not. This SHALL NOT be implemented as two mode-conditional code paths —
migrations have no explicit "shared vs. standalone" signal to branch on;
the guard's behavior is an emergent property of the connecting role's
privileges. The standalone compose profile SHALL continue to use the same
`timescale/timescaledb-ha` image already bundling `postgis`, so standalone
users never hit the failure path.

#### Scenario: PostGIS missing
- **WHEN** the migration runs against a database without `postgis`
  installed and the connecting role lacks `CREATE` on the database
- **THEN** it aborts before creating any object, with an error naming the
  extension and pointing at the platform bootstrap requirement, rather than
  failing later at table-creation time

#### Scenario: Standalone stranger setup
- **WHEN** a new user runs the standalone profile and migrations on a fresh
  volume
- **THEN** `postgis` is created automatically and migrations complete
  without manual intervention

### Requirement: Per-activity heart-rate correlation view
The schema SHALL define `health.strava_activity_hr`, a plain SQL view (not
materialized, not a continuous aggregate) with one row per
`strava_activity` row, exposing `hr_sample_count`, `hr_bpm_sum`,
`hr_bpm_avg`, `hr_bpm_min`, and `hr_bpm_max` computed by joining
`health.heart_rate` samples falling within `[start_time, start_time +
elapsed_time)` for that activity — the **elapsed**-time span, because heart
rate is recorded on wall-clock time through pauses; a moving-time window
would end early and bias the average toward the run's warm-up. The join SHALL be a `LEFT JOIN` so every
activity has a row, with `hr_sample_count = 0` and the other HR columns
`NULL` when no heart-rate samples fall in its window. Neither
`backfill/strava.py` nor `sync/strava_poller.py` SHALL query
`health.heart_rate`; this correlation SHALL exist only as a read-side
Postgres view, never as data written by Strava ingestion.

#### Scenario: Covered activity gets HR stats
- **WHEN** a `strava_activity` row's window overlaps one or more
  `health.heart_rate` rows
- **THEN** `strava_activity_hr` reports that activity's `hr_sample_count`,
  `hr_bpm_avg`, `hr_bpm_min`, and `hr_bpm_max` from exactly those samples

#### Scenario: Uncovered activity still appears
- **WHEN** a `strava_activity` row's window overlaps no `health.heart_rate`
  rows (no device worn, or no Fitbit data synced yet for that period)
- **THEN** `strava_activity_hr` still has a row for that activity, with
  `hr_sample_count = 0` and the bpm columns `NULL`, not simply absent

#### Scenario: Always reflects current data, never stale
- **WHEN** new `health.heart_rate` rows are upserted for a time range that
  overlaps a previously-uncovered activity's window
- **THEN** the next read of `strava_activity_hr` for that activity reflects
  the new coverage immediately, with no refresh step required
