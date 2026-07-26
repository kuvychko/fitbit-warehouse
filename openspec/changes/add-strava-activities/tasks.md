## 1. Cross-repo prerequisite (tracked, not blocking standalone work)

- [ ] 1.1 Confirm companion `homelab` change adds `postgis` to the platform
      bootstrap and `warehouse-cluster-contract.md` (mirrors the existing
      `timescaledb_toolkit` guarantee); do not start shared-mode migration
      work here until it has landed
- [x] 1.2 Verify `postgis` is actually available in the pinned
      `timescale/timescaledb-ha:pg17.10-ts2.28.2` image
      (`SELECT * FROM pg_available_extensions WHERE name = 'postgis'`)

## 2. Schema

- [x] 2.1 Write `infra/migrations/005_strava.sql`: `strava_activity`
      **plain table** (unique constraint on Strava activity ID — not a
      hypertable, see design.md Decision 1) with the full column set per the
      health-schema spec (`start_time` UTC, `utc_offset_s`, `activity_type`,
      `name`, `distance_m`, both `moving_time_s` and `elapsed_time_s`,
      `elev_gain_m`/`elev_loss_m`, `source`); `activity_track` **hypertable**
      partitioned on `time` (unique constraint on `activity_id, time`) with
      `lat`/`lon`/`elevation_m`, a `source` column (outside the key), a
      generated `geography(Point, 4326)` column and GiST index, an explicit
      `chunk_time_interval => INTERVAL '1 month'`, and a compression policy
      (`compress_after => INTERVAL '90 days'`, `compress_orderby =
      'activity_id, time'`, no segmentby) — see design.md Decision 4
- [x] 2.2 Add the PostGIS platform-extension guard, copying the
      `timescaledb_toolkit` guard idiom in
      `infra/migrations/004_analytics.sql` verbatim (try
      `CREATE EXTENSION IF NOT EXISTS postgis` catching
      `insufficient_privilege`, then unconditionally verify via
      `pg_extension` and `RAISE EXCEPTION` if absent) — no mode-conditional
      branching, see design.md Decision 4
- [x] 2.3 Add a `time_col: str | None` field to `backfill/db.py`'s
      `TableSpec` (default `None`, falling back to `key[0]` when unset);
      update `Loader.table_stats()` and `sync/poller.py`'s `window_for()`
      to use it instead of assuming `key[0]` is always a time column (see
      design.md Decision 6 / Risks)
- [x] 2.4 Add `strava_activity` (`time_col="start_time"`) and
      `activity_track` (`time_col="time"`) `TableSpec` entries to
      `backfill/db.py`'s `TABLES` registry
- [x] 2.5 Apply the migration standalone; confirm the hypertable/plain-table
      split, unique constraints, GiST index, and extension guard all behave
      as specced — including that `strava_activity` is *not* a hypertable
      and that a `UNIQUE(activity_id)`-only constraint on it succeeds
- [x] 2.6 Add `health.strava_activity_hr` (plain `VIEW`, not materialized)
      to `005_strava.sql`: `LEFT JOIN` against `health.heart_rate` on
      `[start_time, start_time + elapsed_time)` (elapsed, not moving — see
      design.md Decision 9), exposing
      `hr_sample_count`/`hr_bpm_sum`/`hr_bpm_avg`/`hr_bpm_min`/`hr_bpm_max`
      per activity; confirm `health_ro` can read it without extra grants
      (Postgres's `ALTER DEFAULT PRIVILEGES ... GRANT ON TABLES` from
      migration 001 already covers views)
- [x] 2.7 **Spike before relying on the generated column**: on the pinned
      `timescale/timescaledb-ha:pg17.10-ts2.28.2` image, confirm a
      `GENERATED ... STORED geography` column plus a GiST index survives
      compression (create → insert → compress a chunk → run a geo query) — if
      not, fall back to a plain write-time `geog` column or query-time
      geography from `lat`/`lon` (see design.md Decision 4)

## 3. Backfill parser

- [x] 3.1 Confirm the export's `activities.csv` exposes `sport_type` at the
      same granularity as the API so backfill and sync filter the same set
      (design.md Decision 3). RESOLVED: the CSV has no `sport_type` column
      (only coarse `Activity Type`; the `Type` column is empty), so both
      paths filter on the coarse type, default `Run,Hike`.
- [x] 3.2 Write `backfill/strava.py`: parse `activities.csv`, filter by
      `STRAVA_ACTIVITY_TYPES`, map rows to `strava_activity`
- [x] 3.3 Parse matching `activities/*.gpx` files into `activity_track` rows
      (`lat`/`lon`/`elevation_m` unmodified), deriving each point's `time`
      canonically as `start_time + round(trkpt_time - start_time)` — the same
      rule the sync poller uses — so GPX-loaded and API-synced tracks share
      identical keys (design.md Decision 10)
- [x] 3.4 Report activities with no matching GPX (and vice versa) loudly,
      per `strava-backfill` spec
- [x] 3.5 Wire into `backfill/__main__.py` (new `--strava-dir`/
      `STRAVA_EXPORT_DIR` source, same classify/run_streams shape as the
      Fitbit/Google Fit sources)
- [x] 3.6 Run against the real export (`data/strava_export_121192350.zip`,
      extracted); verify row counts and `MIN`/`MAX(time)` against the raw
      files; confirm idempotent re-run adds nothing

## 4. Sync auth

- [x] 4.1 Write `sync/strava_authorize.py`: one-time interactive OAuth
      (Single Player Mode app, `activity:read_all` scope), token stored at
      `STRAVA_TOKEN_PATH` (gitignored)
- [x] 4.2 Implement rotation-safe refresh: every refresh call rewrites the
      stored token file with the *new* `refresh_token`, not just the
      `access_token`, writing it **atomically (temp file + `os.replace`) and
      before the access token is used** so a crash can't strand the poller —
      do not copy `sync/authorize.py`'s refresh helper unmodified (see
      design.md Decision 5)
- [x] 4.3 Add a test asserting the stored token file's `refresh_token`
      value changes after a refresh call
- [x] 4.4 Make `STRAVA_API_BASE` a `.env`-driven config value, not a
      hardcoded constant

## 5. Sync poller

- [x] 5.1 Write `sync/strava_poller.py`: DB-driven catch-up cursor off
      `max(start_time) - STRAVA_OVERLAP_HOURS` (default 24h) in
      `strava_activity`, mirroring `window_for()`'s overlap in
      `sync/poller.py` (design.md Decision 6)
- [x] 5.2 Pull the activity list via `/athlete/activities?after=...`
      (paginated), filter client-side to `STRAVA_ACTIVITY_TYPES`, upsert
      `strava_activity`
- [x] 5.3 For activities without an existing track, pull
      `/activities/{id}/streams?keys=time,latlng,altitude&key_by_type=true`
      and upsert `activity_track`, deriving absolute time from
      `start_time + offset`
- [x] 5.4 Reuse the existing `CycleAbort`-on-429/error pattern; no new
      backoff machinery
- [x] 5.5 Wire up Healthchecks ping (success/fail) consistent with
      `sync/poller.py`
- [x] 5.6 Add a second `strava-sync` Compose service reusing the sync image
      with command `python -m sync.strava_poller`, its own
      `STRAVA_POLL_INTERVAL` (default ~21600s/6h) and Healthchecks ping
      (design.md Decision 11)
- [ ] 5.7 Run a full cycle end-to-end (including a forced token refresh)
      before enabling the ~6h schedule

## 6. Running dashboard

- [x] 6.1 Write `infra/grafana/dashboards/health-running.json`: 7-day and
      30-day stat-tile groups plus a weekly-mileage bar chart and a
      recent-runs table, mirroring the Scoreboard dashboard's panel shapes,
      with a `$unit_system` template variable (default imperial: mi, min/mi)
      and a `$timezone` textbox like the other dashboards (design.md
      Decision 8)
- [x] 6.2 Distance tile: `sum(distance_m)` over allowlisted activities with
      `start_time` in the window, displayed in the selected distance unit
      (mi by default)
- [x] 6.3 Average pace tile: `sum(moving_time_s) / sum(distance_m)`
      (distance-weighted), rendered as min/mi by default — not an average of
      each run's own pace
- [x] 6.4 Average heart-rate tile: read `health.strava_activity_hr`
      (task 2.6), joined to `strava_activity` and filtered to the window —
      `sum(hr_bpm_sum) / sum(hr_sample_count)` across matching activities
      (sample-weighted, not average-of-averages), plus an explicit "N/M
      runs covered" indicator (`count(*) FILTER (WHERE hr_sample_count >
      0)` vs. `count(*)`) alongside the number
- [x] 6.5 Weekly-mileage bar chart: `sum(distance_m)` bucketed by civil week
      in `$timezone` over a trailing ~16 weeks, in the selected unit
- [x] 6.6 Recent-runs table: one row per allowlisted activity in the window —
      date, distance, pace, elevation gain, average HR (blank when
      uncovered), name
- [x] 6.7 Empty-window and zero-HR-coverage states render as no-data, not
      as a misleading zero (see health-dashboards spec scenarios)
- [ ] 6.8 Load the dashboard against the backfilled export data; spot-check
      one 7-day and one 30-day tile against a hand-computed SQL query

## 7. Config and docs

- [x] 7.1 Add new `.env.example` variables: `STRAVA_CLIENT_ID`,
      `STRAVA_CLIENT_SECRET`, `STRAVA_TOKEN_PATH`, `STRAVA_API_BASE`,
      `STRAVA_ACTIVITY_TYPES` (default `Run,Hike`), `STRAVA_EXPORT_DIR`,
      `STRAVA_POLL_INTERVAL` (default 21600), `STRAVA_OVERLAP_HOURS`
      (default 24), and a Strava-specific Healthchecks ping URL
- [x] 7.2 Write `docs/strava-format.md` (bulk export shape, mirroring
      `docs/takeout-format.md`) — synthesized example data only, no real
      GPS coordinates or activity data
- [x] 7.3 Add Strava API quirks/notes (token rotation, base-URL migration
      date, rate limits) alongside `docs/health-api-notes.md`
- [x] 7.4 Update README's reproduction path to mention the Strava source as
      optional, consistent with the "rare data sources are optional" rule

## 8. Verification

- [ ] 8.1 Confirm standalone mode works end-to-end with no `homelab`
      dependency (PostGIS self-installs, backfill + sync run against the
      bundled DB)
- [ ] 8.2 Once the `homelab` PostGIS platform change has landed, apply
      migration `005` in shared mode and confirm the fail-fast guard is
      gone (extension present) rather than re-verifying the failure path
      against production
- [x] 8.3 Spot-check a geo query against `activity_track`'s geography
      column (e.g. bounding-box or distance query) to confirm the column
      and index are actually usable, not just present
