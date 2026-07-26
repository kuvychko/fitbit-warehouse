## Why

Running data currently lives only in Strava, outside the warehouse. A bulk
account export is already on hand, and the same backfill-then-sync pattern
that brought Fitbit data in applies directly: load the export once, then poll
Strava's API on a cadence so new runs keep landing without manual exports.
GPS track data also opens up geo analysis (route history, heatmaps) that
today's device-metric-only schema has no room for.

## What Changes

- **New backfill parser** (`backfill/strava.py`): reads the bulk Strava
  export's `activities.csv` (per-activity summary: distance, elevation,
  pace, weather, etc.) and `activities/*.gpx` (per-second lat/lon/elevation
  track points), filtered to a configurable activity-type allowlist
  (`STRAVA_ACTIVITY_TYPES`, seeded with `Run,Hike`). No gear/shoe data, no
  original FIT file preservation — only what the bulk GPX export already
  contains.
- **New sync poller** (`sync/strava_poller.py` + `sync/strava_authorize.py`):
  polls Strava's API v3 every ~6h. DB-driven catch-up cursor on
  `strava_activity.start_time`, mirroring `sync/poller.py`'s `window_for()`.
  Pulls the activity list (`/athlete/activities?after=...`), then per new
  activity pulls the GPS stream (`/activities/{id}/streams`). API base URL
  is `.env`-configurable ahead of Strava's Jan 2027 endpoint migration.
- **New OAuth handling with token-rotation**: Strava issues a new
  `refresh_token` on every refresh (unlike the existing Google OAuth flow,
  whose refresh token is stable) — the sync module must persist the latest
  refresh token — atomically, and before the new access token is used —
  after every single refresh call, not just at initial authorization, so a
  crash mid-refresh can't strand the poller.
- **Two new tables** in the `health` schema: `strava_activity` (a plain,
  non-hypertable table — activity summaries, keyed on the immutable Strava
  activity ID; the first ID-keyed table in this schema, and deliberately
  not a hypertable since Timescale requires unique constraints on a
  hypertable to include its partitioning column, which the activity ID
  isn't) and `activity_track` (a hypertable, per-second GPS points, keyed
  on `(activity_id, time)`, partitioned on `time`).
- **PostGIS dependency, platform-gated**: `activity_track` carries a
  generated `geography(Point, 4326)` column with a GiST index alongside
  plain `lat`/`lon`/`elevation_m` floats, to keep geo analysis genuinely
  open rather than aspirational. Migrations check for `postgis` and fail
  fast in shared mode rather than creating it themselves (mirrors the
  existing `timescaledb_toolkit` platform-responsibility pattern). This
  repo's shared-mode migration is **blocked on** a companion `homelab`
  change that adds `postgis` to the platform bootstrap.
- No coupling to Fitbit biometric data at write time — pace/HR correlation
  is a query-time join against `health.heart_rate` by time range, keeping
  the two ingestion paths independent. Materialized as a plain (non-
  materialized-view, non-continuous-aggregate) SQL view,
  `health.strava_activity_hr`, so the join logic lives once in Postgres
  rather than being duplicated per dashboard query or precomputed in
  Python at ingestion time (which was considered and rejected — see
  design.md Decision 9).
- **New Running dashboard** (`health-running.json`): trailing 7-day and
  30-day rolling distance, distance-weighted average pace (min/mi by default,
  via a `$unit_system` variable), and heart-rate averages (read from
  `health.strava_activity_hr`, elapsed-time window, with explicit coverage
  disclosure), plus a weekly-mileage bar chart and a recent-runs table, for
  allowlisted activities.

## Capabilities

### New Capabilities
- `strava-backfill`: one-time parser for the bulk Strava export
  (`activities.csv` + `activities/*.gpx`) into `health.strava_activity` /
  `health.activity_track`, filtered by activity-type allowlist.
- `strava-api-sync`: containerized poller against the Strava API v3 with
  DB-driven catch-up, rotating-refresh-token OAuth, and a configurable API
  base URL.

### Modified Capabilities
- `health-schema`: adds the `strava_activity` (plain table) and
  `activity_track` (hypertable) tables, the `strava_activity_hr` view
  (per-activity heart-rate correlation, read-only, no ingestion coupling),
  and a platform-extension guard requirement (fail-fast check for
  `postgis`, mirroring the existing `timescaledb_toolkit` guard idiom in
  `004_analytics.sql` exactly rather than a mode-conditional variant).
- `health-dashboards`: adds the Running dashboard (7d/30d rolling distance,
  distance-weighted pace in min/mi, and heart rate, plus a weekly-mileage bar
  chart and a recent-runs table) described above.

## Impact

- New files: `backfill/strava.py`, `sync/strava_poller.py`,
  `sync/strava_authorize.py`, a new migration (`infra/migrations/005_*.sql`),
  `infra/grafana/dashboards/health-running.json`, tests alongside
  `tests/test_parsers.py` / `tests/test_poller_mappers.py`.
- `.env.example`: new variables (`STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`,
  `STRAVA_TOKEN_PATH`, `STRAVA_API_BASE`, `STRAVA_ACTIVITY_TYPES`,
  `STRAVA_EXPORT_DIR`, `STRAVA_POLL_INTERVAL`, `STRAVA_OVERLAP_HOURS`, and a
  Strava-specific Healthchecks ping URL).
- `backfill/__main__.py`, `backfill/db.py`: wired to the new stream/table
  registry entries, following the existing `TableSpec` pattern; `TableSpec`
  itself gains an optional `time_col` field so `Loader.table_stats()` and
  `sync/poller.py`'s `window_for()` don't assume the upsert key's first
  column is always a time column (see design.md Decision 6).
- `infra/docker-compose.yml`: a second `strava-sync` service reusing the sync
  image with command `python -m sync.strava_poller` (own interval + ping);
  no `infra/sync.Dockerfile` change beyond including the Strava modules.
- **Cross-repo**: blocked on a companion `homelab` change enabling
  `postgis` in the platform bootstrap (`warehouse-cluster-contract.md`).
  This repo's standalone mode is unaffected by that dependency (same
  `timescaledb-ha` image already bundles `postgis`).
- Docs: `docs/strava-format.md` (bulk export shape, mirroring
  `docs/takeout-format.md`) and `docs/health-api-notes.md`-equivalent notes
  for the Strava API quirks.
