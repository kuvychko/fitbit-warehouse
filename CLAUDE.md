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

## Handling secrets in a session (learned from a real incident, 2026-07)

A production credential-rotation session leaked real values into the
conversation transcript twice, both from the same root mistake: building an
ad-hoc keyword-filtered redaction (`grep -v -iE "PASSWORD|SECRET"` and
similar) before printing a config dump, then having a *different* secret
slip through because its name didn't match the filter (`HEALTH_RO_PW` isn't
caught by a pattern for `PASSWORD`; a `docker compose config` dump redacted
for one variable name printed several others in full, including another
project's credential). The filter is never the fix — the fix is not
printing the dump at all.

- **Never display a file or command output that *might* contain secrets,
  "redacted" or not.** If you need to check a value, check the *fact* you
  actually need instead: does the key exist (`grep -c '^KEY='`), does it
  match a known-good value (hash both sides — `md5sum`/`sha256sum` — and
  compare hashes, never print either value), does authentication succeed
  (a real connection test, which also verifies more than a static compare).
- This applies to `.env` files, `docker compose config` (resolves and
  prints every interpolated value, including ones you weren't asking
  about), `env | grep`, shell history, and any tool's "here's what I'm
  about to do" dry-run output that echoes resolved secrets.
- Generating a new password on Windows/Git-Bash: verify it's clean before
  use (`od -c file | tail -2`, checking for a stray trailing `\r`).
  `tr -d '/+=\n'` does not reliably strip a trailing newline in every
  Windows shell environment, and a stray `\r` is interpreted
  *inconsistently* across tools (bash `$(cat file)` strips trailing `\n`
  only; Docker Compose's own `.env` parser strips `\r` too) — the same
  byte sequence can authenticate in one code path and fail in another,
  which is a much more confusing failure than a bad password would be.
- Never move a secret to a new host "just in case" or as a side effect of
  debugging something else. Each transfer is a new place it can leak or be
  forgotten — move only what's needed, delete it immediately after use,
  and if a plan requires touching a credential nobody asked about, stop
  and say so before doing it.

## Known correctness pitfalls in this codebase

- **`timestamptz` column compared against a bare `(now() AT TIME ZONE
  '$timezone')::date`**: the `::date` cast throws away the timezone
  context, so the later implicit date→timestamptz cast (needed to compare
  against the timestamptz column) uses the *session's* timezone, not
  `$timezone` — silently shifting every civil-day boundary by the UTC
  offset. Re-localize explicitly instead:
  `(date_expr::timestamp AT TIME ZONE '$timezone')`. Found and fixed in
  `health-morning.json` and `health-scoreboard.json` (the WHO AZM panel).
  `health-trends.json` looked similar at a glance but turned out clean on
  closer inspection — it filters via Grafana's `$__timeFilter()` macro
  instead of manual date arithmetic, and already uses the correct
  `(day::timestamp AT TIME ZONE '$timezone')` idiom for output. Don't
  assume a match on the surface pattern is actually a bug — check whether
  the expression is being used for a *comparison* (where the cast
  direction matters) or just *output conversion* (where it doesn't).
- **`infra/migrations/001_bootstrap.sql`'s `ALTER ROLE ... PASSWORD`
  lines run unconditionally on every `migrate` invocation** — unlike the
  `CREATE ROLE` guard above them, they are *not* skipped when the role
  already exists. Running `migrate` (or replaying `001_bootstrap.sql`
  directly) always resets `health_owner`/`health_rw`/`health_ro` to
  whatever values it's given. Rotating credentials outside of a full
  `migrate` run means the *next* `migrate` run needs the exact current
  values passed in, or it will silently roll the rotation back.
- **Docker Compose's `.env` resolution is tied to the compose file's
  directory** (the "project directory"), not the shell's current working
  directory — `docker compose -f infra/docker-compose.yml up ...` run
  from the repo root will *not* find a `.env` that lives at the repo root
  if the compose file's own directory (`infra/`) has none. Symptom looks
  like a missing/empty env var, not a path error. Use an explicit
  `--env-file <path>` when the compose file and `.env` aren't in the same
  directory.
