# Proposal: health-tenant-foundation

## Why

Years of Fitbit health history (heart rate, sleep, steps, HRV, SpO2, …) sit locked
in Fitbit's cloud, and the legacy Fitbit Web API shuts down in **September 2026** —
existing self-hosted tools are InfluxDB-based and mid-migration. This project puts
that history into TimescaleDB/PostgreSQL (queryable SQL, Grafana-ready) and keeps
it current via the new Google Health API, starting from a clean, public-shareable
foundation.

## What Changes

- **Database foundation**: schema `health` with least-privilege roles
  (`health_owner` / `health_rw` / `health_ro`), idempotent bootstrap migrations,
  hypertables for the core Fitbit metrics; deployable standalone (bundled DB) or
  against a shared TimescaleDB cluster (`PG_HOST`).
- **Takeout backfill**: a one-time loader that parses a Google Takeout Fitbit
  export (per-day JSON) and bulk-loads full history — idempotent, re-runnable.
- **Google Health API sync**: a containerized poller (targets a Raspberry Pi) that
  authenticates via Google OAuth, pulls recent data daily with a catch-up window,
  and upserts into the same tables; dead-man-switch monitoring via Healthchecks.io.
- **Dashboards**: Grafana provisioning (datasource on `health_ro` + starter
  dashboards) so history and live data render seamlessly across the
  backfill↔sync seam.

## Capabilities

### New Capabilities
- `health-schema`: the `health` schema, roles, and hypertable layout — the
  contract both writers (backfill, sync) and readers (Grafana) depend on.
- `takeout-backfill`: parsing and bulk-loading a Google Takeout Fitbit export.
- `health-api-sync`: ongoing polling of the Google Health API, OAuth handling,
  idempotent upserts, and sync monitoring.
- `health-dashboards`: Grafana datasource + dashboard provisioning over `health_ro`.

### Modified Capabilities
_None — this is the project's first change; no existing specs._

## Impact

- New code throughout this (currently empty) repo: `infra/` (compose, migrations,
  Grafana provisioning), `backfill/`, `sync/`.
- External dependencies: Google Takeout export (user-requested, generation can take
  hours–days — request early); a Google Cloud project + OAuth client in testing
  mode (personal use). **Spike risk**: confirm testing-mode OAuth suffices for the
  Google Health API before building the poller.
- Private-deployment side (out of scope here, near-zero work): tenant onboarding to
  the author's shared cluster per its contract; full-DB backups already cover any
  new schema.
