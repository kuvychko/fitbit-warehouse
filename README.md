# fitbit-warehouse

Pull your complete Fitbit health history into **TimescaleDB** (PostgreSQL), and keep it
current with an automated sync job — so your heart rate, sleep, steps, HRV, and SpO2
data lives in *your* database, queryable with real SQL and dashboarded with Grafana.

> **Status: early days.** The design is settling and implementation is starting.
> Watch/star if you're interested — feedback and issues welcome.

## Why

- **Your data, your database.** Fitbit keeps years of your health data; getting it out
  in a usable, queryable form shouldn't require a SaaS subscription.
- **The API landscape just shifted.** The legacy Fitbit Web API shuts down in
  September 2026. This project targets the new **Google Health API** from day one —
  no migration debt.
- **SQL, not just dashboards.** Existing self-hosted tools (which are great!) are
  built on InfluxDB. A PostgreSQL/TimescaleDB schema means joins, window functions,
  and integration with everything else that speaks Postgres.

## How it works

```
one-time                                   ┌────────────────────────────┐
┌───────────────┐                          │  TimescaleDB (PostgreSQL)  │
│ Google Takeout│── backfill loader ──────▶│  schema: health            │
│ Fitbit export │   (full history)         │  hypertables: heart_rate,  │
└───────────────┘                          │  sleep, steps, spo2, hrv…  │
ongoing                                    │                            │
┌───────────────┐                          │                            │
│ Google Health │── sync poller ──────────▶│                            │
│ API (OAuth)   │   (daily + catch-up)     └──────────┬─────────────────┘
└───────────────┘   idempotent upserts                │
                                               ┌──────▼──────┐
                                               │   Grafana   │
                                               └─────────────┘
```

- **Backfill**: parse the per-day JSON files from a
  [Google Takeout Fitbit export](https://support.google.com/fitbit/answer/14236615)
  and bulk-load full history — no API rate limits.
- **Sync**: a small containerized poller (runs fine on a Raspberry Pi) pulls recent
  data from the Google Health API and upserts it. The backfill/sync seam is
  idempotent by design, so overlaps are harmless.
- **Database**: schema-per-project with least-privilege roles
  (`health_owner` / `health_rw` / `health_ro`) — designed to coexist as a tenant in a
  shared TimescaleDB instance, but works against a standalone one too.

## Dashboards

Four dashboards are provisioned out of the box (Grafana → folder **Health**):

- **Health Overview** — the original at-a-glance view: heart rate, resting HR,
  steps, sleep, HRV over the last 90 days.
- **Health Trends** — multi-year view: heart-rate percentile bands (not raw
  min/max), resting HR, weight, sleep, HRV, and activity volume, with
  device-era background shading and a row of activity/weight/sleep/RHR
  correlation scatter plots.
- **Health Scoreboard** — a 10–30 day motivational view: streaks, a daily
  steps-vs-goal strip, week-over-week deltas against your own 30-day
  baseline, and personal bests.
- **Morning Report** — last night's hypnogram and sleep score, recovery
  metrics (HRV, resting HR, breathing rate, skin temperature) against your
  30-day baseline, and a "data as of" freshness indicator so a pre-sync
  morning reads as *still syncing*, not stale-as-fresh.

Trends and Scoreboard are powered by hourly continuous aggregates and a
shared 30-day-baseline view (migration `004_analytics.sql`), which need
`timescaledb_toolkit` — see Requirements below. The XY-chart correlation
panels on the Trends dashboard need **Grafana 10+**; the bundled standalone
Grafana (11.4) already satisfies this, but a shared/self-managed Grafana
instance should confirm its version.

## Requirements

- A Fitbit account (migrated to Google sign-in)
- A Google Cloud project with an OAuth client (personal/testing mode is fine)
- Docker; a TimescaleDB instance with the `timescaledb_toolkit` extension
  available (compose file included for standalone use — it pulls
  `timescale/timescaledb-ha`, which ships the extension; a shared/self-managed
  instance needs `timescaledb_toolkit` installed by its admin)

> **Upgrading an existing standalone volume (pre-2026-07):** the standalone
> `db` image moved from `timescale/timescaledb` (Alpine) to
> `timescale/timescaledb-ha` (Ubuntu-based, ships `timescaledb_toolkit`). The
> underlying C library changes between those images, so an existing `db-data`
> volume **cannot** be reused — `docker compose down` the old stack, remove
> the `db-data` volume, and bring the standalone profile back up on a fresh
> volume. Both the backfill loader and the migrations are idempotent, so
> re-running the backfill against your original Takeout export (and letting
> the sync poller catch up) fully restores your data; nothing is lost as long
> as you still have the Takeout export.

This project doesn't implement backups — that's your responsibility (a
per-schema `pg_dump -n health` silently drops hypertable chunk data; back up
the whole database). If you restore a `pg_dump` into a fresh instance, the
target needs `timescaledb_toolkit` installed *before* the restore, or the
restore of the continuous-aggregate objects in migration 004 will fail.

## License

[MIT](LICENSE)
