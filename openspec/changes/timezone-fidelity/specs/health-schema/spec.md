# health-schema (delta)

## ADDED Requirements

### Requirement: Per-sample UTC offset retained
The list-fed metric tables (`heart_rate`, `spo2`, `hrv`, `azm`, `weight`,
`body_fat`, `sleep_session`, `sleep_stage`) SHALL have a nullable
`utc_offset_s integer` column recording the local-time offset (seconds) the
measurement was experienced at, where the source provides it. NULL means
"unknown — assume home zone". The migration SHALL be additive and safe on
compressed hypertables (nullable, no default).

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
