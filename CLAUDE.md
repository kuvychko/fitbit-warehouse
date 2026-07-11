# Guidance for Claude Code in this repository

## What this repo is

`fitbit-warehouse` is a **public** project that loads Fitbit health data into
TimescaleDB and keeps it current:

- **Backfill**: one-time loader for a Google Takeout Fitbit export (per-day JSON →
  hypertables). Full history, no API rate limits.
- **Sync**: containerized poller against the **Google Health API** (the legacy Fitbit
  Web API shuts down September 2026 — never build against it). Runs on a Raspberry Pi;
  daily pull + catch-up window; idempotent upserts so backfill/sync overlap is harmless.
- **Dashboards**: Grafana over the `health` schema via a read-only role.

## Audience: any Fitbit / Google Health user, not just the author

Treat this as a product for strangers, not a personal pipeline. Concretely:

- **Docs follow the new-user journey**, in this order: stand up the DB → request
  and download the Takeout export(s) → run the backfill → set up the API sync
  on a cadence → wire up Grafana dashboards/reports. Write every doc from that
  user's perspective; the author's homelab is never a prerequisite.
- **No private-data leakage in docs**: examples, sample rows, screenshots, and
  doc snippets use synthesized data only (same rule as tests/fixtures below).
- **Rare data sources are optional, not required**: the Basis Peak / Google Fit
  import will apply to few users — keep it a clearly-marked optional path that
  the main flow never depends on.
- **DB backups: required, but out of scope**. Docs must call out that users need
  their own full-database backup policy (per-schema `pg_dump -n` silently drops
  hypertable chunks). In the author's deployment this is owned by the private
  `homelab` repo — this project only documents the need, it never implements
  backups.

## Warehouse tenant context (private deployment)

In the author's homelab this repo is a *tenant* of a shared TimescaleDB cluster
(`warehouse-db` on a NAS), governed by the warehouse cluster contract in the private
`homelab` repo. Conventions inherited from that contract:

- Schema = `health`; roles `health_owner` / `health_rw` / `health_ro`;
  role `search_path = health, public` (TimescaleDB functions live in `public`).
- Two-mode deployment: standalone DB via compose profile, or shared via `PG_HOST`.
- Tenant repos NEVER define the shared DB container; migrations bootstrap
  schema/roles idempotently (`\gexec`-guarded CREATE ROLE / CREATE SCHEMA — plain
  `CREATE SCHEMA IF NOT EXISTS` fails without database CREATE privilege).
- Hypertables: `create_hypertable()` after table creation; remember per-schema
  `pg_dump -n` silently omits hypertable chunks — backups must be full-database.

## Conventions & hygiene

- **Public repo, personal health data**: never commit secrets, `.env`, OAuth tokens,
  Takeout exports, or any real health data (including in tests/fixtures — synthesize
  test data). `.gitignore` + `.env.example` + gitleaks pre-commit are in place.
- Config 100% via `.env`; committed `.env.example` documents every variable.
- Keep docs public-friendly: a stranger with a Fitbit account and Docker should be
  able to follow the README without knowing anything about the author's homelab.
- Dev context is Windows (PowerShell primary, Bash available); deploy target is
  Linux (Raspberry Pi). Keep scripts portable; `.gitattributes` forces LF for
  `*.sh` / `*.sql`.
