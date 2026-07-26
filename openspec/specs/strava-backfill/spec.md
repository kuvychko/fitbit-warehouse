# strava-backfill

## Purpose

One-time loader for a bulk Strava account export (`activities.csv` +
`activities/*.gpx`) into `health.strava_activity` / `health.activity_track`.
An optional data source, independent of the Fitbit Takeout backfill.
Implemented in `backfill/strava.py`, wired into `backfill/__main__.py` via
`--strava-dir` / `STRAVA_EXPORT_DIR`. Added by change `add-strava-activities`
(deployed 2026-07-25).

## Requirements

### Requirement: One-time load from a bulk Strava export
The backfill loader SHALL parse a Strava bulk account export (an extracted
directory containing `activities.csv` and `activities/*.gpx`) and load
matching activities into `health.strava_activity` and
`health.activity_track`, without using any rate-limited API.

#### Scenario: Full export loads
- **WHEN** the loader runs against an extracted Strava export directory
- **THEN** all activities matching the configured type allowlist are
  ingested and per-table row counts and `MIN(time)`/`MAX(time)` are reported
  for verification against the raw export

### Requirement: Activity-type allowlist
The loader SHALL only import activities whose coarse activity type is in the
configured allowlist (`STRAVA_ACTIVITY_TYPES`, default `Run,Hike`) — read
from the CSV `Activity Type` column, the same coarse type the sync poller
filters on, so the export and ongoing sync include the same set of activities
— and SHALL report excluded activities as intentionally skipped rather than
silently dropping them. (The bulk export carries no granular `sport_type`
column, so the coarse type is the only field that keeps both paths in parity.)

#### Scenario: Non-matching activity skipped
- **WHEN** the export contains an activity whose type is not in the
  allowlist
- **THEN** the activity is not loaded and appears in the run summary as
  skipped by type

### Requirement: CSV summary is authoritative; GPX is track-only
The loader SHALL take activity summary fields (distance, elevation
gain/loss, moving time, elapsed time, and related metrics) from
`activities.csv` without recomputing them from the matching GPX track; it
SHALL store both `moving_time_s` and `elapsed_time_s` (pace is defined on
moving time, the heart-rate correlation window on elapsed time). Each GPX
`<trkpt>` SHALL become one `activity_track` row carrying its `lat`, `lon`,
and `elevation_m` unmodified (no reconciliation against the CSV's corrected
elevation totals), with its `time` derived canonically as the activity's UTC
start plus the point's rounded offset (`start_time + round(trkpt_time -
start_time)`) rather than the raw trkpt timestamp — so a GPX-loaded track and
an API-synced track for the same activity produce identical
`(activity_id, time)` keys and overlap dedups.

#### Scenario: Summary values match the export
- **WHEN** an activity's summary is loaded
- **THEN** `strava_activity` values for distance/elevation/time match
  `activities.csv` exactly, not a value recomputed from the GPX track

#### Scenario: Track points loaded from GPX
- **WHEN** an activity's GPX file is loaded
- **THEN** each `<trkpt>` becomes one `activity_track` row with its latitude,
  longitude, and elevation unmodified, and a `time` derived as the activity's
  UTC start plus the point's rounded offset (identical to how the API sync
  derives it)

### Requirement: Idempotent, resumable loading
The loader SHALL be safe to re-run over the same export (and over an export
overlapping previously synced API data): already-present activities and
track points are absorbed by natural-key upserts, never duplicated.

#### Scenario: Second run adds nothing
- **WHEN** the loader runs twice over the same export
- **THEN** row counts in both tables after the second run equal counts
  after the first

### Requirement: Loud handling of unknown or missing input
The loader SHALL report activities listed in `activities.csv` with no
matching GPX file, and any GPX file with no matching CSV row, rather than
silently dropping either side.

#### Scenario: Missing GPX reported
- **WHEN** an allowlisted activity's CSV row references a GPX filename that
  is not present in the export
- **THEN** the activity's summary still loads and the missing track is
  reported explicitly in the run summary
