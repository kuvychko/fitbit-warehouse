# Design: health-tenant-foundation

## Context

Greenfield public repo. Two data paths into one TimescaleDB schema: a one-time
Google Takeout backfill (full history, no rate limits) and an ongoing Google
Health API poller. Deployment mirrors the proven `env_monitoring` tenant pattern:
two-mode compose (standalone bundled DB vs shared cluster via `PG_HOST`),
idempotent migrations, per-role least privilege. The author's private deployment
runs shared mode against `warehouse-db` on a NAS, with the poller on a
Raspberry Pi 5 that already hosts another tenant's stack.

Hard constraint: the legacy Fitbit Web API is turned down September 2026. All
API work targets the **Google Health API** (Google Cloud project + Google OAuth;
31 data types; `list` / `reconcile` / `rollUp` / `dailyRollUp` read methods;
intraday without special approval).

## Goals / Non-Goals

**Goals:**
- Full Fitbit history in TimescaleDB, verified against the raw export.
- Hands-off ongoing sync with the same alerting discipline as the rest of the
  homelab (Healthchecks.io dead-man switch).
- The backfill↔sync seam is invisible in queries and dashboards.
- Public-usable: a stranger can run standalone mode end-to-end from the README.

**Non-Goals:**
- Not a general multi-vendor health platform (no Garmin/Apple/Oura import).
- No webhook/near-realtime sync in v1 (daily + catch-up polling is enough;
  webhooks need a public endpoint, which conflicts with a private-LAN Pi).
- No write-back to Google (read-only integration).
- No Fitbit Web API (legacy) support at all.

## Decisions

1. **Schema-first, sources adapt.** Tables model *metrics* (heart_rate, sleep,
   steps, …), not sources. Both the Takeout parser and the API poller normalize
   into the same shape. A `source` column (`takeout` | `api`) records provenance
   without splitting tables.

2. **Idempotency via natural time keys.** Every hypertable gets a primary/unique
   key on its natural grain (e.g. `(time)` for per-minute HR, `(date)` for daily
   summaries, `(sleep_start, ...)` for sessions) and all writers use
   `INSERT ... ON CONFLICT DO NOTHING` (or `DO UPDATE` where the API can revise
   recent values — decided per table in specs). Overlap between backfill and
   sync, or between consecutive polls, is therefore harmless by construction.

3. **Exact table list is fixed during implementation, not up front.** Takeout
   directory contents drive Phase A's table set (target: heart rate intraday,
   sleep sessions + stages, steps, SpO2, HRV, breathing rate, AZM, weight if
   present). The `health-schema` spec defines requirements per metric *family*;
   adding a table later is additive, not breaking.

4. **Bootstrap migrations copy the battle-tested patterns**: `\gexec`-guarded
   `CREATE ROLE` / `CREATE SCHEMA` (plain `IF NOT EXISTS` requires database
   CREATE privilege), `search_path = health, public` per role (TimescaleDB
   functions live in `public`), default privileges so `_rw`/`_ro` see future
   tables, `SET ROLE health_owner` for DDL. Migration runner = the same
   pg_isready-wait + `ON_ERROR_STOP` shell pattern as `env_monitoring`.

5. **Backfill is a Python CLI, not a container service.** It runs once, on
   whatever machine holds the Takeout zip (dev laptop), connecting to the DB
   directly. Parse per-day JSON → normalized rows → batched `COPY`/upsert.
   Python (stdlib + psycopg) keeps it readable for a public audience.

6. **Poller is a small Python container** on the Pi: on schedule (daily +
   catch-up window of N days, default 7) pull each configured data type via the
   Health API, upsert, then ping Healthchecks (`/fail` on error) — same trap
   discipline as the warehouse backup job. 429/quota handling: respect
   `Retry-After`, resume next cycle; catch-up window self-heals missed days.

7. **OAuth spike gates the poller build.** First task of Phase B: Google Cloud
   project + OAuth client in *testing* mode, run the auth flow once
   interactively, store the refresh token outside the repo
   (`GOOGLE_TOKEN_PATH`), make one successful data read. If testing-mode
   verification or scopes block personal use, we re-plan (fallback: scheduled
   Takeout re-exports — clunky but viable).

8. **Grafana: provisioning-as-code** (datasource on `health_ro` + starter
   dashboards JSON), same layout as `env_monitoring/infra/grafana`. In the
   author's deployment these load into the existing Pi Grafana; standalone mode
   bundles a Grafana service behind the same compose profile as the DB.

## Risks / Trade-offs

- **Google Health API is weeks old** → docs/quota behavior may shift; the spike
  (Decision 7) and a thin API-client module isolate the blast radius.
- **Takeout format is undocumented and can change** → parser validates
  file-by-file and reports skipped/unknown files loudly rather than guessing;
  loader is re-runnable so partial loads are recoverable.
- **Personal health data in a public repo's orbit** → hard rule: no real data in
  fixtures/tests; `.gitignore` blocks `data/`, `takeout/`, dumps, CSVs, tokens;
  gitleaks pre-commit. Dashboards screenshots for the README must be reviewed
  before committing.
- **DO NOTHING vs DO UPDATE on conflict**: DO NOTHING can freeze a
  provisional value the API later revises (e.g. daily summaries finalize after
  the day ends). Mitigation: summaries at daily grain use DO UPDATE; immutable
  intraday samples use DO NOTHING. Set per table in the schema spec.
- **Pi is a shared host** (already runs the `iaq` stack) → poller is one small
  container on a schedule; no new always-on services beyond it.
