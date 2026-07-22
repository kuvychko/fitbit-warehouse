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

## Quickstart (standalone mode)

Standalone mode bundles TimescaleDB + Grafana in Docker — no shared cluster
needed. You'll need Docker and Python 3.10+.

1. **Clone and configure:**
   ```
   git clone https://github.com/<you>/fitbit-warehouse.git
   cd fitbit-warehouse
   cp .env.example .env
   ```
   Edit `.env`: set `POSTGRES_SUPER_PW`, `HEALTH_OWNER_PW`, `HEALTH_RW_PW`,
   `HEALTH_RO_PW`, and `GRAFANA_ADMIN_PW` to your own values (anything
   sufficiently random — these stay local to your containers). Leave
   `PG_HOST=db` and the standalone profile handles the rest.

2. **Start the database + Grafana, then apply migrations:**
   ```
   docker compose --profile standalone up -d
   docker compose run --rm migrate
   ```
   Migrations are idempotent — safe to re-run any time (e.g. after pulling
   an update). Grafana is now live at `http://localhost:3000`
   (`admin` / your `GRAFANA_ADMIN_PW`) with the `health` datasource and all
   four dashboards provisioned, just empty until data lands.

3. **Backfill your history:**
   [Request your Google Takeout Fitbit export](https://support.google.com/fitbit/answer/14236615)
   (pick "Google Health" data; generation can take hours) and extract it. Then:
   ```
   python -m venv .venv && .venv/Scripts/activate   # Windows; use bin/activate on Linux/macOS
   pip install -r requirements.txt
   ```
   Set `TAKEOUT_DIR`, `TAKEOUT_TZ` (your Fitbit profile's IANA timezone), and
   `TAKEOUT_WEIGHT_UNIT` in `.env` to match your export, then run:
   ```
   python -m backfill
   ```
   It's idempotent — re-running (e.g. after adding a later Takeout export)
   only ever upserts, never duplicates. See `docs/takeout-format.md` for what
   each stream maps to.

4. **Keep it current with the sync poller (optional but recommended):**
   Create a Google Cloud project + OAuth 2.0 **Desktop app** client for the
   Google Health API, then authorize once:
   ```
   python -m sync.authorize
   ```
   This opens a browser for consent and stores a refresh token at
   `GOOGLE_TOKEN_PATH`. Set `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in
   `.env`, then either run `python -m sync.poller` directly or containerize it:
   ```
   docker compose --profile sync up -d --build sync
   ```
   **Use "In production" publishing status on the OAuth consent screen, not
   Testing** — Testing-mode refresh tokens silently expire after 7 days and
   the poller's cycles start failing; production (even unverified, for a
   personal-use client) doesn't have that cap. See
   `docs/health-api-notes.md` for the full spike writeup.

Steps 3 and 4 are independent and can run in either order — the backfill/sync
seam is idempotent by design.

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
- A Google Cloud project with an OAuth client (personal use is fine; use "In
  production" publishing status, not Testing — see Quickstart step 4)
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
