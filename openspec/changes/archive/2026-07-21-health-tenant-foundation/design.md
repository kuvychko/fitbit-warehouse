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

The actual Takeout exports (pulled July 2026, verified complete) shape the scope:

- **Fitbit ("Google Health") export, 482 MB**: 2016-10-02 → present. Ships most
  streams in *two parallel formats*: classic per-day JSON ("Global Export
  Data") and newer per-day CSVs ("Physical Activity_GoogleData") with ISO-8601
  UTC timestamps, a device `data source` column, and per-stream readmes. The
  classic JSON has a trap: timestamp *values* are UTC but files are bounded by
  *local* day. Heart rate runs at ~5–15 s resolution (~20–30 M rows over the
  decade). Sleep stage detail (`levels`) and exercise logs exist only in JSON.
- **Google Fit export, 11 MB**: contains the surviving **Basis Peak** archive —
  per-minute heart rate (149 k points), calories, steps, and activity segments
  for 2015-07-03 → 2015-11-02 — as Google Fit "Data Points" JSON
  (`startTimeNanos`, `fpVal`/`intVal`). A dense baseline from a decade ago,
  ending ~11 months before the Fitbit history starts.

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
   steps, …), not sources. All writers — the Fitbit Takeout parser, the Google
   Fit/Basis parser, the API poller — normalize into the same shape. Provenance
   lives in columns, not tables: `source`
   (`fitbit-takeout` | `googlefit-takeout` | `api`) plus a `device` column
   where the data carries it ("Charge 5", "Basis Peak"). "Resting HR 2015 vs
   2026" is then a plain query over one table.

2. **Idempotency via natural time keys.** Every hypertable gets a primary/unique
   key on its natural grain (e.g. `(time)` for per-minute HR, `(date)` for daily
   summaries, `(sleep_start, ...)` for sessions) and all writers use
   `INSERT ... ON CONFLICT DO NOTHING` (or `DO UPDATE` where the API can revise
   recent values — decided per table in specs). Overlap between backfill and
   sync, or between consecutive polls, is therefore harmless by construction.

3. **Table list driven by what the export actually contains.** Confirmed
   present and worth importing (tier 1): heart rate intraday, resting HR, sleep
   sessions + stages + sleep score, steps, calories/distance/activity levels,
   SpO2, HRV, breathing rate, skin temperature, AZM, weight/body fat. Small but
   cheap extras (tier 2, add opportunistically): VO2max, Daily Readiness,
   Stress Score, ECG readings, exercise sessions, mindfulness/EDA. Explicitly
   *skipped*: GPS/location samples and live pace (bulky, low insight, most
   privacy-sensitive), swim lengths, glucose (empty in this export), menstrual
   health (n/a), social/notification/commerce cruft — the loader reports these
   as intentionally skipped rather than unknown. The `health-schema` spec
   defines requirements per metric *family*; adding a table later is additive,
   not breaking. Volume is modest (tens of millions of intraday HR rows);
   enable native Timescale compression on intraday hypertables.

4. **Bootstrap migrations copy the battle-tested patterns**: `\gexec`-guarded
   `CREATE ROLE` / `CREATE SCHEMA` (plain `IF NOT EXISTS` requires database
   CREATE privilege), `search_path = health, public` per role (TimescaleDB
   functions live in `public`), default privileges so `_rw`/`_ro` see future
   tables, `SET ROLE health_owner` for DDL. Migration runner = the same
   pg_isready-wait + `ON_ERROR_STOP` shell pattern as `env_monitoring`.

5. **Backfill is a Python CLI, not a container service.** It runs once, on
   whatever machine holds the Takeout zip (dev laptop), connecting to the DB
   directly. Parse → normalized rows → batched `COPY`/upsert. **CSV-first**:
   where a stream exists in both Fitbit formats, load the newer CSVs
   (unambiguous UTC timestamps, device source column) and skip the classic
   JSON duplicate; use JSON only for CSV-less streams (sleep `levels`/stages,
   exercise logs). A second, small parser module handles the Google Fit export
   (Basis Peak streams — same tables, `source = 'googlefit-takeout'`).
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

8. **UTC everywhere.** Every time column is `timestamptz` stored as UTC. The
   newer CSVs and the Google Fit nanosecond epochs are already UTC; the classic
   JSON's UTC-values/local-day-file-boundary quirk is a parser concern,
   documented in `docs/takeout-format.md`. Local-time rendering is Grafana's
   job.

9. **Grafana: provisioning-as-code** (datasource on `health_ro` + starter
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
