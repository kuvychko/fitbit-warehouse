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
last night and previous day, identifying "last night" via
`health.primary_sleep_session` (never `sleep_session.main_sleep` directly,
which is unset for API-synced nights): a merged hypnogram+heart-rate overlay,
sleep duration (efficiency omitted — Takeout-only, see design.md D14
correction), a readiness composite, recovery metrics (HRV, resting heart
rate, breathing rate, overnight SpO2, nightly skin-temperature deviation)
each shown against its `daily_baseline` median with deviation direction, a
nap indicator, today's weigh-in, today's live activity so far, a
bedtime-anchored heart-rate curve for the 24h before sleep and a
wake-anchored one for today so far (both locked to `primary_sleep_session`
rather than a calendar day — see design.md D15), yesterday's activity
recap, and a "data as of" freshness indicator reflecting the newest synced
sample.

#### Scenario: Fresh by morning
- **WHEN** the 2-hourly poller has synced last night's sleep by ~08:00 local
- **THEN** the report shows last night's data and the freshness stat confirms
  its recency

#### Scenario: Sync not yet landed
- **WHEN** the report is viewed before last night's data has synced
- **THEN** the freshness indicator makes the staleness obvious instead of
  silently showing the prior night as current

#### Scenario: Report resolves the sync-only night, not the last backfilled one
- **WHEN** every `sleep_session` row for the last several nights was written
  by the API poller (`main_sleep IS NULL` on all of them)
- **THEN** the report still shows last night — not the most recent
  Takeout-backfilled night — because it resolves "last night" through
  `primary_sleep_session`

### Requirement: Hypnogram and heart rate as one overlay, locked to the actual sleep event
The hypnogram and nighttime heart rate SHALL render in a single panel, not
as separate panels, with sleep stages visually distinguishable. The panel's
visible extent SHALL be locked to the actual `primary_sleep_session` for the
most recent night — exactly, not approximately — regardless of what time of
day the dashboard is viewed. **Not a Grafana time-range mechanism**: every
approach that routed through Grafana's "now"-relative time picker (a
variable-interpolated time range, a relative panel-level override, a wider
dashboard default) drifted depending on viewing time, because these
panels' real anchor is a fixed past event, not "now" — see design.md D15.
The panel instead uses a numeric X axis (hours relative to sleep onset)
computed in SQL, which cannot drift because it never references "now" at
all. Sleep-stage distinction is per-point coloring (each heart-rate sample
tagged with its containing stage), not background bands — Grafana's native
region-annotation overlay (the original mechanism, design.md D12) requires
a time-domain axis incompatible with the numeric one this fix depends on.

#### Scenario: Stages are visually distinct
- **WHEN** last night includes deep, light, REM, and wake segments
- **THEN** each is distinguishable by color on the heart-rate trace

#### Scenario: Same night, viewed at different times of day
- **WHEN** the dashboard is loaded once in the morning and again that
  evening, with no new sleep session in between
- **THEN** both views show the identical sleep session and window — neither
  the framing nor the underlying data differs by viewing time

#### Scenario: Hour-offset axis has a stated wall-clock reference
- **WHEN** a user looks at the hypnogram, "Yesterday's heart rate," or
  "Today's heart rate" panel, each showing hours relative to sleep onset or
  wake rather than clock time
- **THEN** the actual bedtime and wake clock times those offsets are
  relative to are visible elsewhere on the dashboard, not left implicit

### Requirement: Readiness composite replaces the live Sleep Score
Because Fitbit's proprietary Sleep Score has no equivalent in the Google
Health API sync payload, the morning report SHALL NOT rely on
`health.sleep_score` for nights synced via the API. Instead it SHALL show a
readiness composite computed from `daily_baseline` deviations across HRV,
resting heart rate, and sleep minutes — the metrics genuinely live via the
API sync (breathing rate, nightly temperature, and SpO2 are excluded: the
first two are Takeout-only with no API puller, and SpO2's baseline would
require a schema change reverted during implementation for a performance
regression — see design.md D11/D14): a smoothed average ("vibe") and a
worst-single-metric caution indicator that a good average cannot mask.

#### Scenario: One bad metric surfaces despite good others
- **WHEN** resting heart rate deviation is well outside its baseline
  p25–p75 but every other component is within its normal range
- **THEN** the caution indicator flags it even though the averaged vibe
  score alone would read as normal

#### Scenario: Historical Sleep Score remains available elsewhere
- **WHEN** a user looks at Trends or Scoreboard for a Takeout-backfilled
  period
- **THEN** the original Fitbit Sleep Score is still queryable from
  `health.sleep_score` as before — only the morning report's live framing
  changes

### Requirement: Today's activity and recovery add-ons
The morning report SHALL show a live cumulative stat for today's
steps/AZM/calories so far (sourced from the real-time-enabled hourly
aggregates), overnight SpO2 average/min for the primary sleep window, a nap
indicator when a non-primary `sleep_session` row exists for the same day,
and today's weigh-in when one exists.

#### Scenario: Today's activity updates through the day
- **WHEN** the dashboard is viewed mid-afternoon
- **THEN** the activity stat reflects steps/AZM/calories accumulated since
  midnight local, not zero and not yesterday's total

#### Scenario: No nap, no weigh-in yet
- **WHEN** there is no secondary sleep session today and no weight entry yet
  today
- **THEN** those panels read as absent/not-yet, not as zero or an error
