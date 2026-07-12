# health-api-sync (delta)

## ADDED Requirements

### Requirement: UTC offsets stored, never discarded
The poller SHALL populate `utc_offset_s` from the API's per-sample
`utcOffset` (session tables use the interval's start offset; sleep stages
their own segment offsets). Rows from sources without offsets (rollups)
leave it NULL.

#### Scenario: Travel is visible in the data
- **WHEN** samples are synced from a period in a different timezone
- **THEN** their `utc_offset_s` differs from home-zone rows, and the change
  point is queryable

### Requirement: Daily steps rollup sync
The poller SHALL pull `steps` via `dailyRollUp` into `steps_daily`, taking
`day` from the response's civil date, within the same DB-driven catch-up
window as other streams; a one-time bootstrap mode SHALL walk the full
Fitbit-era history in range-cap-sized chunks.

#### Scenario: Trip week matches the app
- **WHEN** a week spans a timezone change
- **THEN** `steps_daily` values for those days equal the Fitbit app's daily
  totals

#### Scenario: Bootstrap is idempotent
- **WHEN** the deep history pull runs twice
- **THEN** row counts are unchanged after the second run
