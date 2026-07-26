## ADDED Requirements

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
