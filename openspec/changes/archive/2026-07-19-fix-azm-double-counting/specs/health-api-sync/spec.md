## ADDED Requirements

### Requirement: AZM values stored as raw, unweighted zone minutes
The poller SHALL normalize Active Zone Minutes values from the Google
Health API to raw, unweighted, per-clock-minute zone occupancy before
storing them in `health.azm.minutes` — the same semantics the Takeout CSV
backfill path already stores. `health.azm.minutes` SHALL carry this meaning
regardless of `source`, since dashboard queries apply the CARDIO/PEAK 2x
AZM weighting themselves and assume unweighted input.

#### Scenario: Cardio/peak minutes are not pre-weighted
- **WHEN** the API reports a data point for a CARDIO or PEAK zone interval
- **THEN** the stored `health.azm.minutes` value reflects raw clock-minutes
  in that zone, not the zone-weighted score

#### Scenario: Dashboard AZM total matches the Fitbit app
- **WHEN** a day's `health.azm` rows (any mix of sources) are summed with
  the dashboard's CARDIO/PEAK 2x weighting query
- **THEN** the result matches the Active Zone Minutes total shown in the
  Fitbit app for that day, within rounding
