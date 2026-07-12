# health-dashboards

## Purpose

Grafana provisioning over the read-only role: datasource + starter
dashboards rendering the full history seamlessly across backfill and API
sync. Implemented in `infra/grafana/`; served standalone or mounted into a
host Grafana (the author's Pi runs one Grafana for two tenants via a
platform-owned overlay).

## Requirements

### Requirement: Provisioned read-only datasource
Grafana provisioning SHALL define a PostgreSQL datasource for the `health`
schema using the `health_ro` role — dashboards can never write.

#### Scenario: Datasource healthy and read-only
- **WHEN** Grafana starts with the provisioning files and a valid `.env`
- **THEN** the datasource health check passes and the connected role cannot
  `INSERT`

### Requirement: Device-aware daily aggregates
Dashboard queries that aggregate intraday streams into daily totals SHALL NOT
naively sum across concurrent recording devices (the export interleaves
wearable and phone streams at offset timestamps, so all survive the natural
key): they aggregate per device first and take the maximum, or filter to a
single device. Daily bucket boundaries SHALL follow a user-visible timezone
variable, not UTC.

#### Scenario: Concurrent wearable and phone streams
- **WHEN** a day contains steps rows from both a wearable and a phone stream
- **THEN** the steps-per-day panel shows approximately the wearable's total,
  not the sum of all streams

### Requirement: Starter dashboards spanning the seam
The project SHALL ship at least one provisioned dashboard covering the core
metrics (heart rate, sleep, steps) that renders continuously across the
Takeout-backfill → API-sync boundary.

#### Scenario: No visible seam
- **WHEN** history was backfilled from Takeout and recent days come from the
  API poller
- **THEN** panels spanning the boundary date render without gaps or duplicate
  artifacts

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
