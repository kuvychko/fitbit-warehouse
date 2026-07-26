## ADDED Requirements

### Requirement: Running dashboard (7d/30d rolling stats)
The project SHALL ship a provisioned Running dashboard
(`health-running.json`) showing trailing 7-day and 30-day rolling stats for
allowlisted Strava activities (`STRAVA_ACTIVITY_TYPES`): total distance,
distance-weighted average pace, and heart-rate-coverage-weighted average
heart rate. Pace SHALL be shown in **min/mi** and distance in **miles** by
default (via a `$unit_system` template variable defaulting to imperial,
switchable to metric → km, min/km), because runners train by pace, not by
km/h. Distance and pace SHALL be sourced from `health.strava_activity`
summary columns (`distance_m`, `moving_time_s`), never recomputed from
`health.activity_track` points; pace is distance-weighted
(`sum(moving_time_s) / sum(distance_m)`), not an average of per-run paces.
Average heart rate SHALL be computed via a query-time join against
`health.heart_rate` over each activity's `[start_time, start_time +
elapsed_time)` window — never a stored column on the Strava side — consistent
with this project's write-time separation between the Strava and Fitbit
ingestion paths.

#### Scenario: Rolling windows update as new runs land
- **WHEN** a new allowlisted run is synced with a `start_time` within the
  trailing 30 days
- **THEN** the 30-day distance total includes it, and the 7-day tiles include
  it once its `start_time` is also within the trailing 7 days

#### Scenario: Distance-weighted pace, not run-averaged
- **WHEN** a window contains both a long slow run and a short fast run
- **THEN** the average pace tile equals total moving time divided by total
  distance across the window, not the unweighted average of each run's own
  pace

#### Scenario: No activity in window shows absence, not zero
- **WHEN** a window (7-day or 30-day) contains no allowlisted activity
- **THEN** the dashboard's tiles for that window read as no-data, not as a
  zero distance/speed/heart-rate figure

### Requirement: Training-history panels (weekly mileage and recent runs)
Beyond the trailing-window stat tiles, the Running dashboard SHALL include a
weekly-mileage bar chart and a recent-runs table, so the data reads as a
training history rather than only two scalars. The weekly bar chart SHALL
bucket `sum(distance_m)` by civil week in the dashboard's `$timezone`, in the
selected distance unit, over a trailing multi-week window. The recent-runs
table SHALL list one row per allowlisted activity over the dashboard window
with its date, distance, pace, elevation gain, average heart rate (blank when
uncovered), and name.

#### Scenario: Weekly bars reflect civil weeks
- **WHEN** the dashboard renders
- **THEN** each bar is one civil week's total distance in the selected unit,
  bucketed in `$timezone`, not a rolling or UTC-bucketed total

#### Scenario: Recent-runs table shows per-run detail
- **WHEN** the window contains allowlisted runs
- **THEN** each appears as its own row with date, distance, pace, elevation
  gain, average heart rate (blank if no overlapping heart-rate data), and name

### Requirement: Heart-rate tile states its own coverage
The Running dashboard's heart-rate tile SHALL display, alongside its
averaged bpm value, how many of the window's runs actually had overlapping
`health.heart_rate` data — because Strava activities carry no biometric
columns of their own. A window with partial heart-rate coverage SHALL NOT
present its average as if every run in the window contributed to it.

#### Scenario: Partial coverage disclosed
- **WHEN** a 7-day window has 5 qualifying runs but only 3 have overlapping
  heart-rate samples
- **THEN** the tile shows the average alongside a "3/5 runs" (or equivalent)
  coverage indicator

#### Scenario: Zero coverage shows absent, not a misleading zero
- **WHEN** a window's qualifying runs have no overlapping heart-rate data at
  all
- **THEN** the heart-rate tile reads as no-data rather than 0 bpm, while the
  distance and speed tiles for the same window still render normally
