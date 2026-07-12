# health-api-sync

## Purpose

Ongoing polling of the Google Health API: OAuth handling, per-data-type
pullers, idempotent upserts, and dead-man monitoring. Implemented in `sync/`
(container on the Pi, 2 h cadence); live since 2026-07-12. API behavior
notes: `docs/health-api-notes.md`.

## Requirements

### Requirement: Google Health API integration only
The sync poller SHALL authenticate with Google OAuth 2.0 (refresh token stored
outside the repo at `GOOGLE_TOKEN_PATH`) and read data exclusively from the
Google Health API. The legacy Fitbit Web API SHALL NOT be used.

#### Scenario: Authenticated read
- **WHEN** the poller runs with a valid stored refresh token
- **THEN** it obtains an access token non-interactively and reads configured
  data types successfully

### Requirement: Scheduled sync with catch-up window
The poller SHALL run on a schedule (default daily) and, on each run, pull a
trailing catch-up window (default 7 days) for every configured data type,
upserting by natural key so missed runs self-heal and revised recent values
(e.g. finalized daily summaries) are corrected.

#### Scenario: Missed runs self-heal
- **WHEN** the poller was down for 3 days and then runs
- **THEN** the missed days within the catch-up window are present afterward

#### Scenario: Overlapping polls do not duplicate
- **WHEN** two consecutive runs cover overlapping date ranges
- **THEN** overlapping rows are not duplicated

### Requirement: Rate-limit and quota tolerance
The poller SHALL treat HTTP 429/quota responses as expected conditions: honor
any `Retry-After`, stop the current cycle gracefully, and rely on the next
cycle's catch-up window — never busy-retry or crash-loop.

#### Scenario: Quota exhausted mid-run
- **WHEN** the API returns 429 partway through a run
- **THEN** the run ends without error spam and the next run backfills the gap

### Requirement: Dead-man-switch monitoring
The poller SHALL ping a configured Healthchecks-style endpoint only after a
fully successful cycle and ping the failure endpoint on error, so silent
breakage (crash, auth expiry, host down) raises an alert. An unset URL disables
pinging without affecting sync.

#### Scenario: Failure alerts
- **WHEN** a cycle fails or the poller stops running
- **THEN** no success ping is sent and the endpoint's alert fires

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
