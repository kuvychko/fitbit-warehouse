# health-dashboards (delta)

## ADDED Requirements

### Requirement: Steps panel uses Fitbit civil days
The steps-per-day panel SHALL read `steps_daily` (max per source per day)
instead of bucketing intraday sums, so daily totals match the Fitbit app
across timezone travel. The panel SHALL NOT change values for non-travel
periods beyond the dedup already in place.

#### Scenario: No regression at home
- **WHEN** comparing a non-travel week before and after the switch
- **THEN** daily totals agree within rounding

#### Scenario: Travel week renders as lived
- **WHEN** the time range covers a trip across timezones
- **THEN** each bar equals the app's total for that local day (no split
  days)
