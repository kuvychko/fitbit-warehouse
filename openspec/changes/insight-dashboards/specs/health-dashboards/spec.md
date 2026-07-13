## ADDED Requirements

### Requirement: Trends dashboard (multi-year)
The project SHALL ship a provisioned Trends dashboard rendering multi-year
history: heart-rate percentile bands (median line with p25–p75 band from
`heart_rate_hourly` sketches — never raw min/max), resting heart rate, weight,
sleep duration and composition, nightly HRV, and activity volume, with
smoothing (rolling medians/bands) appropriate to multi-year zoom. Daily
bucketing SHALL follow the dashboard timezone variable.

#### Scenario: Decade renders interactively
- **WHEN** the time range spans the full history (~10 years)
- **THEN** every panel renders from aggregates/daily tables without scanning
  raw intraday chunks, fast enough for interactive use

#### Scenario: Robust to sensor glitches
- **WHEN** the raw stream contains isolated implausible bpm spikes
- **THEN** the heart-rate trend bands are visually unaffected

### Requirement: Device-era background regions
The Trends dashboard SHALL render each wearable's active era as a translucent
background annotation region (distinct hue per device), derived from
heart-rate presence per device, so measurement-method step-changes at device
transitions are visually attributable and never read as health events.

#### Scenario: Device transition
- **WHEN** the visible range includes a switch between wearables
- **THEN** the background tint changes at the transition and identifies both
  devices

### Requirement: Correlation row with explicit lag direction
The Trends dashboard SHALL include a row of scatter (XY) panels: trailing-28d
average steps vs weigh-in weight, previous-day activity vs that night's sleep
score, and trailing-28d activity vs resting heart rate. Each panel title SHALL
state its lag direction; points SHALL be colored by year; no causal language.

#### Scenario: Lag direction visible
- **WHEN** a user reads any correlation panel
- **THEN** the title states which variable trails which (e.g., "28d steps →
  weigh-in"), and point color communicates the year

### Requirement: Scoreboard dashboard (10–30 days)
The project SHALL ship a provisioned Scoreboard dashboard for the trailing
10–30 days: a calendar heatmap of daily steps, current-streak counters,
week-over-week deltas, personal bests for the period, and weekly active-zone
minutes against the WHO 150 min/week guideline. Comparisons SHALL be framed
against the user's own trailing baseline from `daily_baseline`, not fixed
absolute goals (except the WHO panel).

#### Scenario: Streak reflects civil days
- **WHEN** daily steps cross the streak threshold on consecutive civil days in
  the dashboard timezone
- **THEN** the streak counter equals the number of consecutive qualifying days
  ending today

#### Scenario: Baseline-relative framing
- **WHEN** a metric this week is below the trailing 30-day median
- **THEN** the tile shows the deviation from that median, not from a fixed goal

### Requirement: Morning report dashboard
The project SHALL ship a provisioned Morning Report dashboard covering the
last night and previous day: a hypnogram from `sleep_stage`, sleep score and
efficiency, recovery metrics (HRV, resting heart rate, breathing rate, nightly
skin-temperature deviation) each shown against its `daily_baseline` median
with deviation direction, yesterday's heart-rate curve and activity recap, and
a "data as of" freshness indicator reflecting the newest synced sample.

#### Scenario: Fresh by morning
- **WHEN** the 2-hourly poller has synced last night's sleep by ~08:00 local
- **THEN** the report shows last night's data and the freshness stat confirms
  its recency

#### Scenario: Sync not yet landed
- **WHEN** the report is viewed before last night's data has synced
- **THEN** the freshness indicator makes the staleness obvious instead of
  silently showing the prior night as current
