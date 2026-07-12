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
