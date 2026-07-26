# strava-api-sync

## Purpose

Containerized poller against the Strava API v3, keeping
`health.strava_activity` / `health.activity_track` current with new
running/hiking activities and their GPS tracks. Implemented in
`sync/strava_poller.py` + `sync/strava_authorize.py`; runs on the Raspberry
Pi as the `strava-sync` compose service alongside the Google Health `sync`
poller. Added by change `add-strava-activities` (deployed 2026-07-25).

## Requirements

### Requirement: Strava API integration only
The sync poller SHALL authenticate with Strava OAuth 2.0 (Single Player Mode
app, `activity:read_all` scope) and read data exclusively from the
configured Strava API base URL (`STRAVA_API_BASE`, `.env`-configurable, not
hardcoded), so the January 2027 base-URL migration is a configuration
change, not a code change.

#### Scenario: Authenticated read
- **WHEN** the poller runs with a valid stored refresh token
- **THEN** it obtains an access token non-interactively and reads the
  activity list successfully

#### Scenario: Base URL is configurable
- **WHEN** `STRAVA_API_BASE` is set to a different value
- **THEN** the poller issues requests against that base URL without any
  code change

### Requirement: Refresh-token rotation is persisted every cycle
The poller SHALL persist the latest `refresh_token` returned by the token
endpoint back to the stored token file after every single refresh call —
including refreshes that happen automatically within a poll cycle, not only
the initial interactive authorization. This matters because Strava issues a
new `refresh_token` on every refresh, unlike this project's existing Google
OAuth flow. The new token SHALL be persisted **atomically** (write a temp
file, then replace) and **before** the fresh access token is used for any
fallible request, so a crash mid-write or mid-cycle can never leave a
truncated or already-superseded token file — which would lock the poller out
and force manual re-authorization.

#### Scenario: Refresh token updates after a cycle
- **WHEN** a poll cycle completes and has refreshed the access token
- **THEN** the stored token file's `refresh_token` value differs from its
  value before the cycle started

#### Scenario: Crash after refresh does not strand the poller
- **WHEN** the process is interrupted immediately after a refresh call
  returns a new refresh token
- **THEN** the stored token file already contains the new, valid refresh
  token (persisted before it was used), never a truncated or superseded one

#### Scenario: Second cycle succeeds after rotation
- **WHEN** two consecutive poll cycles each trigger a token refresh
- **THEN** the second cycle authenticates successfully using the refresh
  token persisted by the first cycle

### Requirement: Scheduled sync with DB-driven catch-up
The poller SHALL run on a schedule (default ~6 hours) and, on each run,
determine its catch-up window from `max(start_time)` already stored in
`health.strava_activity` minus an overlap margin (`STRAVA_OVERLAP_HOURS`,
default 24h, so an activity uploaded near the previous cursor is never
skipped), pull activities newer than that cursor via the activity-list
endpoint (`after`, paginated via `page`/`per_page`), filter to the configured
activity-type allowlist (on the coarse `type`, matching the backfill), and
upsert by natural key so missed cycles self-heal.

#### Scenario: Missed cycles self-heal
- **WHEN** the poller was down for 2 days and then runs
- **THEN** activities from the missed period are present afterward

#### Scenario: Overlapping polls do not duplicate
- **WHEN** two consecutive cycles cover an overlapping time window
- **THEN** overlapping activities and track points are not duplicated

### Requirement: GPS stream pulled for new activities
The poller SHALL request the GPS stream (`/activities/{id}/streams` with
`keys=time,latlng,altitude` and `key_by_type=true`) for each activity not
yet present in `health.activity_track`, and upsert the resulting points,
deriving each point's absolute timestamp as `start_time + round(offset_s)` —
the same canonical rule the backfill uses, so a track already loaded from a
GPX export is not duplicated. Skipping activities that already have track
rows is a required correctness guard, not only an optimization.

#### Scenario: New activity gets a track
- **WHEN** a newly synced activity has a GPS stream available
- **THEN** `health.activity_track` contains one row per stream point with a
  correctly derived absolute timestamp

#### Scenario: Backfilled activity is not re-fetched or duplicated
- **WHEN** an activity already has track points (e.g. loaded by the backfill)
- **THEN** the poller does not pull its stream again, and no duplicate track
  points are created

### Requirement: Rate-limit tolerance
The poller SHALL treat HTTP 429 responses as an expected condition: end the
current cycle without error, and rely on the next cycle's catch-up window —
never busy-retry or crash-loop.

#### Scenario: Rate limit hit mid-cycle
- **WHEN** Strava returns 429 partway through a cycle
- **THEN** the cycle ends cleanly and the next cycle's catch-up window
  covers the gap

### Requirement: Dead-man-switch monitoring
The poller SHALL ping a configured Healthchecks-style endpoint only after a
fully successful cycle and ping the failure endpoint on error, consistent
with the existing Fitbit sync poller's monitoring discipline.

#### Scenario: Failure alerts
- **WHEN** a cycle fails or the poller stops running
- **THEN** no success ping is sent and the endpoint's alert fires
