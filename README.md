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

## Requirements

- A Fitbit account (migrated to Google sign-in)
- A Google Cloud project with an OAuth client (personal/testing mode is fine)
- Docker; a TimescaleDB instance (compose file included for standalone use)

## License

[MIT](LICENSE)
