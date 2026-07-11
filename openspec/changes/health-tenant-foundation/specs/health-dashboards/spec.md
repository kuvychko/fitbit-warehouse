# health-dashboards (delta)

## ADDED Requirements

### Requirement: Provisioned read-only datasource
Grafana provisioning SHALL define a PostgreSQL datasource for the `health`
schema using the `health_ro` role — dashboards can never write.

#### Scenario: Datasource healthy and read-only
- **WHEN** Grafana starts with the provisioning files and a valid `.env`
- **THEN** the datasource health check passes and the connected role cannot
  `INSERT`

### Requirement: Starter dashboards spanning the seam
The project SHALL ship at least one provisioned dashboard covering the core
metrics (heart rate, sleep, steps) that renders continuously across the
Takeout-backfill → API-sync boundary.

#### Scenario: No visible seam
- **WHEN** history was backfilled from Takeout and recent days come from the
  API poller
- **THEN** panels spanning the boundary date render without gaps or duplicate
  artifacts
