# Design: add-strava-activities

## Context

Second data source in the warehouse, following the exact backfill+sync split
`health-tenant-foundation` established for Fitbit. A bulk Strava account
export (`data/strava_export_121192350.zip`) is already on hand and was
inspected directly while scoping this change:

- `activities.csv`: 332 rows, **all `Run`**. Rich per-activity summary —
  distance, moving/elapsed time, elevation gain/loss, grade, cadence,
  power, weather, sunrise/sunset — already computed by Strava, no need to
  re-derive from the track.
- `activities/*.gpx`: one file per activity (filename = Strava activity ID),
  `<trkpt>` per GPS fix at roughly 1 Hz. No `<extensions>` block observed on
  any sampled file — no embedded HR/cadence/power. A 68-minute run produced
  4,018 points; across 332 activities that's on the order of 700K–1.3M rows
  from backfill alone.
- Everything else in the export (clubs, kudos, followers, gear, shoes,
  comments, media) is social-graph data, explicitly out of scope.

The API side targets Strava API v3 (`https://www.strava.com/api/v3`, moving
to `https://api-v3.strava.com` 2027-01-04 — kept in `.env`, never hardcoded).
Single Player Mode app, `activity:read_all` scope. Access tokens expire every
6h; **every refresh rotates the refresh token**, unlike the existing Google
Health OAuth flow (`sync/authorize.py`) whose refresh token is stable across
refreshes — that module's shape cannot be copied unmodified.

The warehouse runs on a shared TimescaleDB cluster (`warehouse-db`, image
`timescale/timescaledb-ha:pg17.10-ts2.28.2`) governed by the private
`homelab` repo's cluster contract. That contract already has a precedent for
a database-level extension that isn't schema-scoped:
`timescaledb_toolkit` is installed once by the platform, and tenant
migrations only check for it and fail fast in shared mode. `postgis` needs
the identical treatment — this repo's migration cannot safely
`CREATE EXTENSION postgis` itself against a shared instance it doesn't own.

## Goals / Non-Goals

**Goals:**
- Full Strava running (and hiking) history in `health`, backfilled from the
  export already in hand.
- Ongoing sync on a ~6h cadence, same catch-up-cursor discipline as
  `sync/poller.py`.
- GPS track data preserved at full fidelity (no simplification/downsampling)
  so geo analysis is genuinely available later, not aspirational.
- Strava data stays queryable against Fitbit biometrics via plain time-range
  joins, without merging the two sources at write time.
- A provisioned Running dashboard shows trailing 7-day and 30-day rolling
  distance, distance-weighted average pace (min/mi), and average heart rate,
  plus a weekly-mileage trend and a recent-runs table, so the new data is
  actually visible somewhere on day one rather than only queryable by hand.

**Non-Goals:**
- No gear/shoe/equipment tracking.
- No original file preservation (FIT or otherwise) — the bulk GPX export is
  the ceiling for backfill fidelity; sync uses Strava's `/streams` endpoint
  directly, also not original files.
- No biometric columns (HR/cadence/power) on the Strava side, even though
  the live API can return them — out of scope until a real need appears;
  correlation is a join, not a merge.
- No geo/map dashboard panels (route rendering, heatmaps) in this change —
  the Running dashboard is stat tiles plus a weekly-mileage bar chart and a
  recent-runs table, but no map; `activity_track`'s geography column exists
  for future geo work, per the design's stated goal of keeping that path
  genuinely open, not because this change's dashboard needs it.
- No webhook subscription — polling only, matching the Fitbit sync's
  precedent and this being a single-account integration.

## Decisions

1. **One new hypertable plus one plain table, not a reuse of existing
   metric tables.** `health.strava_activity` is a **plain (non-hypertable)
   table**, one row per activity, keyed on the immutable Strava activity ID,
   action `"update"` so edits/renames propagate. `health.activity_track`
   **is** a hypertable, one row per GPS fix, keyed on `(activity_id, time)`,
   partitioned on `time`, action `"nothing"` — immutable samples. `time` on
   `activity_track` is derived at write time (`activity.start_time +
   stream_offset_seconds`) so it stays a normal `timestamptz` hypertable
   column, consistent with every other table, even though Strava's own
   stream format is offset-indexed.

   `strava_activity` is deliberately **not** a hypertable, unlike every
   other table `db.TABLES` registers. Two independent reasons:
   - TimescaleDB requires any unique/primary-key constraint on a hypertable
     to include the partitioning column. Every existing hypertable in this
     schema satisfies that trivially because its natural key *is* its time
     column (see `infra/migrations/001_bootstrap.sql` /
     `002_hypertables.sql`). `strava_activity`'s natural key is the activity
     ID, not a time column, so a `UNIQUE (activity_id)` constraint on a
     `start_time`-partitioned hypertable would fail to create. Making the
     natural key composite (`activity_id, start_time`) just to satisfy
     Timescale would be semantically wrong (an edit that changes
     `start_time` must not create a duplicate row) and isn't worth it for a
     table this size.
   - At the row volumes involved (332 activities in the full export,
     on the order of a few per week going forward), `strava_activity` has
     none of the chunk-and-compress motivation that justifies hypertables
     elsewhere in this schema — it's a small, slow-growing catalog, closer
     in shape to a dimension table than a metric stream.

   This is also the first table in `db.TABLES` whose natural key does not
   start with a time column — `activity_track`'s key `(activity_id, time)`
   shares the same property. `backfill.db.time_col()` currently assumes
   `spec.key[0]` is always a time column (it's used both by
   `Loader.table_stats()`'s verification report and by `sync/poller.py`'s
   `window_for()` catch-up cursor). Both new tables need `TABLES` entries
   that don't break that assumption — see the `TableSpec.time_col` addition
   in the Schema section of `tasks.md`.

2. **`activities.csv` is the summary source of truth for backfill; GPX is
   track-only.** Mirrors the Fitbit parser's CSV-first rule. No
   recomputation of distance/elevation/pace from raw track points — Strava's
   own summary values are authoritative and go straight into
   `strava_activity`; the raw per-point `elevation_m` in `activity_track` is
   kept as-is without reconciling it against `activities.csv`'s
   corrected `elev_gain_m`/`elev_loss_m` (same principle as the existing
   codebase's "don't reconcile summary vs. raw-derived values" pattern).

3. **Activity-type filter is `.env`-driven, not hardcoded, and matches on
   the *coarse* activity type in *both* ingestion paths.**
   `STRAVA_ACTIVITY_TYPES` (default `Run,Hike`), applied identically in
   backfill (filtering `activities.csv` rows) and sync (filtering the
   `/athlete/activities` list client-side — Strava's list endpoint has no
   server-side type filter). Mirrors `--only` in the Fitbit backfill CLI and
   `TAKEOUT_WEIGHT_UNIT` as existing config-first precedent in this codebase.
   The value is stored in an `activity_type` column on `strava_activity`.

   **The filter field is the coarse type, not the granular `sport_type` —
   resolved empirically against the real export.** The bulk export's
   `activities.csv` was inspected directly: it carries a coarse `Activity
   Type` column (value `Run` for all 332 rows) and a literally-named `Type`
   column that is **empty on every row** — it does **not** expose Strava's
   granular `sport_type` (`TrailRun`/`VirtualRun`/…) at all. The API *does*
   return `sport_type`, but choosing it would make backfill and sync select
   *different* sets of activities (the parity bug), because the export can
   only ever supply the coarse type. Both paths therefore filter on the coarse
   type: backfill reads the CSV `Activity Type` column, sync reads the API's
   `type` field (still returned alongside the deprecated-but-present `type`),
   with the same `Run,Hike` allowlist. If Strava ever drops `type` from the
   API, sync derives the coarse value from `sport_type` via a documented
   downcast (`TrailRun`→`Run`, etc.) — the one place a mapping would live.

   Granular `sport_type` is intentionally *not* stored: it is unavailable for
   the backfilled majority of rows, so storing it only for API rows would
   create a half-populated column. Adding it later (API rows only) is a
   trivial, isolated follow-up if a real need appears.

4. **PostGIS: generated column + platform fail-fast guard**, copying the
   *actual* `timescaledb_toolkit` guard idiom in
   `infra/migrations/004_analytics.sql` verbatim rather than reinventing a
   mode-conditional one. That existing guard is mode-agnostic — it doesn't
   branch on "shared" vs. "standalone" at all (there is no such flag
   anywhere in `infra/migrations/run.sh`; migrations only ever see a
   `PG_MIGRATE_USER` connection, and mode is an emergent property of that
   role's privileges, not an explicit signal):
   ```sql
   DO $$
   BEGIN
       CREATE EXTENSION IF NOT EXISTS postgis;
   EXCEPTION WHEN insufficient_privilege THEN
       NULL; -- shared-mode role without CREATE on the database; checked below
   END
   $$;

   DO $$
   BEGIN
       IF NOT EXISTS (SELECT FROM pg_extension WHERE extname = 'postgis') THEN
           RAISE EXCEPTION 'postgis extension is not installed. Shared-mode: '
               'ask the platform admin to run "CREATE EXTENSION postgis" in '
               'the warehouse database (see the warehouse cluster contract). '
               'Standalone: the migrate service must connect as a role with '
               'CREATE privilege on the database (default: postgres).';
       END IF;
   END
   $$;
   ```
   In standalone mode the migrate service's superuser role has `CREATE`, so
   the first block succeeds and the second is a no-op. In shared mode the
   tenant role lacks `CREATE` on the database, the first block's exception
   handler swallows `insufficient_privilege` silently, and the second block
   fails loud if the platform hasn't installed `postgis` yet. One migration
   path, no mode plumbing, identical to how `004_analytics.sql` already
   handles `timescaledb_toolkit`.

   `activity_track` gets `lat float8`, `lon float8`, `elevation_m float8`
   (plain, human-legible, matches every other table's style) **plus** a
   generated `geog geography(Point, 4326)` column (SRID 4326 = WGS84,
   native to GPX/GPS, no reprojection) with a GiST index. It also carries a
   `source text` provenance column (`strava-export` | `strava-api`,
   CHECK-constrained like every other table, **excluded from the natural key**
   so export/API overlap upserts cleanly), keeping the schema's uniform
   provenance convention.

   **Chunk interval and compression are set explicitly, not left to
   defaults.** Backfill spans years of runs at a few per week, so the default
   7-day chunk interval would scatter the data across hundreds of near-empty
   chunks; `activity_track` is created with `chunk_time_interval => INTERVAL
   '1 month'` (matching the low-frequency hypertables in
   `002_hypertables.sql`). Compression is enabled and scheduled with
   `compress_after => INTERVAL '90 days'` (mirroring `002`'s boundary, safely
   outside any catch-up write window), `compress_orderby = 'activity_id,
   time'`, and no `segmentby` (activity_id is too high-cardinality to segment
   on; there is no low-cardinality column to group by).

   **Spike required before committing to the generated column.** Whether
   TimescaleDB native compression coexists with a `GENERATED ... STORED
   geography` column *and* a GiST index is version-sensitive and unverified on
   the pinned `timescale/timescaledb-ha:pg17.10-ts2.28.2` image. Implementation
   MUST verify this end-to-end (create → insert → compress a chunk → geo query
   still works) before relying on the generated column; the fallback, if it
   doesn't, is to drop the generated column and compute geography at query
   time from the plain `lat`/`lon`, or populate a plain (non-generated)
   `geog` column at write time.
   - This repo's shared-mode deployment is **blocked** until a companion
     `homelab` change lands (tracked separately, not part of this change).

5. **Token persistence rewritten for rotation, not copied from
   `sync/authorize.py`.** Every call to Strava's token refresh endpoint
   returns a new `access_token` *and* a new `refresh_token`; the response
   must be written back to `secrets/strava_token.json` after **every**
   refresh — including the ones that happen automatically inside each
   poller cycle, not just the interactive one-time authorization. Reusing
   Google's `refresh_access_token()` shape (which discards everything but
   the access token) would strand the poller after its first cycle: the
   next refresh would present an already-superseded refresh token.

   **Ordering and durability are safety-critical, unlike the Google flow.**
   Strava invalidates the old refresh token server-side the instant it issues
   a new one, so a crash between "refresh succeeded" and "new token persisted"
   permanently locks the poller out (manual re-auth required) — a real hazard
   on the Raspberry Pi's SD-card / yanked-power deployment. The poller MUST
   persist the new token **before** using the fresh access token for any
   fallible work, and MUST write it **atomically** (temp file + `os.replace`)
   so an interrupted write can never leave a truncated/dead token file. Order
   per refresh: exchange → atomic-persist new refresh_token → only then use
   the access token.

6. **Sync poller follows the existing DB-driven catch-up shape, with an
   overlap margin on the cursor.** Cursor =
   `max(start_time) - STRAVA_OVERLAP_HOURS` from `strava_activity` (same idea
   as `window_for()`'s overlap in `sync/poller.py`, default 24h). The raw
   `max(start_time)` alone would set `after` = exactly the newest stored
   start, and a boundary or late-uploaded activity near that instant could be
   skipped; backing the cursor off by an overlap margin closes that gap, and
   re-listing already-stored activities is free because they upsert by ID.
   Each cycle: list activities via
   `/athlete/activities?after=<cursor-epoch>` (paginated via `page`/
   `per_page`), filter client-side to the type allowlist, upsert summaries;
   for activities not already in `activity_track`, pull
   `/activities/{id}/streams?keys=time,latlng,altitude&key_by_type=true`
   and upsert track points. `CycleAbort` on non-200/429 reuses the existing
   pattern in `sync/poller.py` — rate limits (100/15min, 1000/day) are not a
   real constraint at this cadence for one account's activity list, so no
   new backoff machinery beyond what already exists.

   `window_for()` cannot be reused unmodified for `strava_activity`: it
   calls `db.time_col(spec)`, which today returns `spec.key[0]` — the
   *upsert* key's first column, not necessarily a time column. For
   `strava_activity` that's `activity_id`. `backfill/db.py`'s `TableSpec`
   gets a new optional `time_col: str | None` field (falling back to
   `key[0]` when unset, so every existing table is unaffected); `TABLES`
   entries for `strava_activity` (`time_col="start_time"`) and
   `activity_track` (`time_col="time"`) set it explicitly. Both
   `Loader.table_stats()` and `window_for()` switch to calling this field
   instead of assuming `key[0]`.

7. **API base URL is `.env`-configurable** (`STRAVA_API_BASE`, default
   `https://www.strava.com/api/v3`), unlike `sync/authorize.py`'s hardcoded
   `API_BASE` constant for the Google Health API. This is a stricter
   convention than the existing code, adopted specifically because Strava
   has already announced the January 2027 migration — not retrofitted onto
   the Google module in this change.

8. **Running dashboard aggregation semantics, decided explicitly rather
   than left to whoever writes the panel SQL.** New provisioned dashboard
   `infra/grafana/dashboards/health-running.json`, mirroring the existing
   Scoreboard dashboard's trailing-window stat-tile shape
   (`health-scoreboard.json`) rather than the Trends dashboard's
   multi-year framing. Two panel groups, trailing 7 days and trailing 30
   days, each showing:
   - **Distance**: `sum(distance_m)` over allowlisted activities
     (`STRAVA_ACTIVITY_TYPES`) with `start_time` in the window — a
     training-volume total, not a per-run average, since "how far did I
     run this week" is the natural reading. Displayed in **miles**
     (`distance_m / 1609.344`).
   - **Average pace**: distance-weighted, shown as **min/mi** — the unit
     runners actually train by, not km/h. Pace = total moving time over total
     distance (`sum(moving_time_s) / 60 / (sum(distance_m) / 1609.344)`),
     distance-weighted rather than a plain average of each run's own pace — a
     plain average would let a single short, fast run skew the figure
     disproportionately against a week of longer, slower runs. Distance and
     pace units are driven by a `$unit_system` template variable (default
     `imperial` → mi, min/mi; switchable to metric → km, min/km), mirroring
     the existing `$steps_goal`/`$timezone` textbox-variable pattern on the
     Scoreboard dashboard and the `TAKEOUT_WEIGHT_UNIT` config precedent.
   - **Average heart rate**: read from `health.strava_activity_hr` (see
     Decision 9), sample-weighted across every allowlisted activity in the
     period — `sum(hr_bpm_sum) / sum(hr_sample_count)` over the window's
     activities, not an average of each run's own average HR, for the same
     skew reason as speed.
   - Runs with no `heart_rate` coverage for their window (device not worn,
     or an API-synced run from before Fitbit sync existed) contribute
     distance but not HR (`hr_sample_count = 0` in the view). The HR tile
     SHALL display alongside its number how many of the window's runs
     actually had coverage (e.g. "142 bpm avg (3/5 runs)"), never a bare
     average that silently implies full coverage. If a window has zero
     covered runs, the HR tile shows absent/no-data, matching the existing
     "absent, not zero" pattern from the Morning Report's nap/weigh-in
     tiles.

   Beyond the two trailing-window stat groups, the dashboard SHALL also carry
   two time-oriented panels, so the data reads as a training history rather
   than only two scalars:
   - **Weekly mileage bar chart** (trailing ~16 weeks): `sum(distance_m)`
     bucketed by civil week in `$timezone`, in the selected distance unit.
     Training volume is inherently a time series — a ramp is the actual
     question a runner brings — which a 7d/30d scalar cannot show. Mirrors the
     Scoreboard dashboard's existing barchart panels.
   - **Recent runs table**: one row per allowlisted activity over the
     dashboard window — date, distance, pace, elevation gain, average HR (from
     `strava_activity_hr`, blank when uncovered), and name — so the per-run
     detail is present, not aggregated away. Mirrors the Scoreboard's table
     panel.

9. **HR correlation is a Postgres view, not a Python-side precomputed
   join.** Considered and rejected: computing per-activity HR stats in
   `strava_poller.py`/`backfill/strava.py` and writing them into a new
   table keyed on `activity_id`. Rejected because it reintroduces exactly
   the write-time coupling Decision 1/the Goals section rules out — Strava
   ingestion would need to read `health.heart_rate`, and worse, would need
   Fitbit's sync to have already landed the overlapping window before
   Strava's own sync runs, which isn't guaranteed (Fitbit sync runs on its
   own ~2h cadence; Strava's runs on ~6h): a run synced this morning could
   get permanently stale/null HR stats with nothing to ever recompute them,
   since nothing re-triggers Strava's ingestion when new Fitbit data
   arrives later for the same window.

   The correlation window is `[start_time, start_time + elapsed_time)`, using
   **elapsed** time, not moving time. Heart rate is recorded on wall-clock
   time and the watch keeps sampling through pauses (stoplights, water stops),
   so a `moving_time` window would end early — dropping every sample after the
   run's moving-time mark and biasing the average toward the lower warm-up HR
   at the start. `elapsed_time` spans the whole activity as the athlete
   actually lived it. (Average *pace* still divides by `moving_time` —
   Decision 8 — because pace is defined on moving time; only the HR
   correlation window uses elapsed.)

   Instead, `health.strava_activity_hr` (Decision 8's SQL sketch above) is
   a plain SQL `VIEW` — not a `MATERIALIZED VIEW`, and not a TimescaleDB
   continuous aggregate (those require a `time_bucket()` group-by on the
   source hypertable, which doesn't fit "grouped by activity"). A plain
   view has no staleness window and needs no refresh trigger/policy; the
   per-activity join is cheap regardless of `heart_rate`'s total size
   because it's an indexed range scan per activity, and the dashboard's
   rolling windows only ever touch a handful of activities. Neither
   `strava_poller.py` nor `backfill/strava.py` ever queries
   `health.heart_rate` — the view is schema-level (`health-schema`
   capability, created in `005_strava.sql` alongside the two new tables),
   read only by dashboard/query code, and requires no ordering guarantee
   between the two sources' sync cadences. If per-query cost ever becomes
   a real problem (unlikely at current activity volumes), converting it to
   a `MATERIALIZED VIEW` with a periodic `REFRESH` is a mechanical,
   isolated follow-up — the view's column shape doesn't change.

   **Deployment finding (realized risk):** a plain view over a `GROUP BY
   activity_id` does **not** let Postgres push a dashboard's outer
   `start_time` window predicate into the aggregation — so
   `... JOIN strava_activity_hr USING (activity_id) WHERE a.start_time >= …`
   computes HR stats for *every* activity (one `heart_rate` range scan each),
   which at 337 activities over the 39.8M-row production `heart_rate` table
   exceeds Grafana's query timeout, and the HR/recent-runs panels render "No
   data". The `health-dashboards` requirement is "a query-time join over each
   activity's elapsed window" — the *view* is one way to express that, not
   the only one. The Running dashboard therefore uses an inline
   `LEFT JOIN LATERAL` against `health.heart_rate` that filters activities to
   the window *first* (a handful of rows) and correlates only those — same
   elapsed-window semantics, but fast. `strava_activity_hr` is retained for
   ad-hoc single-/small-set lookups (an `activity_id =` equality *does* push
   down), with the materialized-view escape hatch above if an all-activities
   consumer ever appears.

   No per-device filtering in the view: unlike `steps`/`distance`/etc.,
   where the `health-dashboards` spec requires per-device dedup before
   summing (concurrent wearable+phone streams double-count a sum),
   `health-trends.json`'s existing heart-rate panels already roll up
   `bpm_agg` sketches across every device/source with no filtering — an
   average is not distorted by overlapping-but-consistent readings the way
   a sum is. `strava_activity_hr` follows that same precedent. If Strava's
   overlapping HR data turns out to mean something that does skew the
   average (not just multiple concurrent samples of the same real bpm),
   this view is the one place to add a device filter later.

10. **Track-point `time` is derived identically in both ingestion paths, so
    overlap is truly idempotent.** `activity_track`'s natural key is
    `(activity_id, time)` with action `nothing`; if backfill and sync computed
    `time` even one second apart for the same activity, the key would miss and
    the run's points would *double* rather than dedup — breaking the
    idempotency the `strava-backfill` and `strava-api-sync` specs promise. The
    two paths otherwise draw `time` from different sources (backfill: absolute
    `<trkpt><time>` in the GPX; sync: the stream `time` array of
    integer-second offsets), so a single canonical rule is mandated:
    **`time = start_time + round(offset_seconds)`**, where `start_time` is the
    activity's canonical UTC start (from `activities.csv` for backfill,
    `start_date` for sync) and `offset_seconds` is the point's offset from
    that start. Backfill therefore does **not** store raw trkpt timestamps
    directly — it computes each point's offset as `round(trkpt_time -
    start_time)` and rebuilds `time` the same way sync does, so a GPX-loaded
    activity and a later API-synced (or re-backfilled) one produce
    byte-identical keys.

    Sync additionally skips the `/streams` pull entirely for any activity that
    already has rows in `activity_track` — that existence guard is
    **load-bearing**, not just an optimization: it is what keeps a normal sync
    from re-deriving a backfilled run's track. The canonical-derivation rule
    above is the backstop for the one case the guard doesn't cover — a re-run
    of the *backfill* over an export whose activities were first landed by the
    API — where identical `time` keys are the only thing preventing duplicate
    points.

11. **The poller ships as a second Compose service reusing the sync image,
    not a thread in the existing container.** `infra/docker-compose.yml` gains
    a `strava-sync` service that reuses the same image as the Fitbit `sync`
    service with a different command (`python -m sync.strava_poller`), its own
    `STRAVA_POLL_INTERVAL` (default ~21600 s / 6 h) and its own Healthchecks
    ping URL. This mirrors how `backfill` and `sync` already share one image,
    keeps the two cadences (2 h vs 6 h) and two dead-man switches
    independently observable, and lets one poller crash without taking the
    other down — none of which a shared in-process thread would give. No
    change to `infra/sync.Dockerfile` beyond ensuring the Strava modules are
    present in the image.

## Risks / Trade-offs

- **Cross-repo dependency on `homelab`** → this repo's shared-mode migration
  cannot complete until the platform PostGIS bootstrap lands. Mitigation:
  the fail-fast check makes the blocker loud and immediate (migration
  aborts with a clear message) instead of a confusing downstream failure;
  standalone mode is entirely unaffected and can be developed/tested first.
- **Track-table volume (700K–1.3M+ rows from backfill, growing indefinitely)**
  → same shape as the existing heart-rate hypertable, so the precedent for
  handling it (native TimescaleDB compression on the hypertable) applies
  directly; not a new operational problem.
- **~~`type` vs `sport_type` filter field~~ (RESOLVED)** → inspecting the real
  export settled this: `activities.csv` has no `sport_type` column, so both
  paths filter on the coarse activity type (`Run,Hike`) to stay in parity
  (Decision 3). No residual risk beyond Strava eventually dropping the API's
  `type` field, for which the documented `sport_type`→coarse downcast is the
  fallback.
- **Strava GPX privacy**: the bulk export's raw GPS points are not
  necessarily privacy-zone-redacted (this account currently has no privacy
  zones configured). Not a schema concern — DB storage of real GPS is no
  different from any other real health data already in this schema — but a
  hard reminder for anyone touching docs/tests/fixtures for this feature:
  no real coordinates in anything committed, synthesize a fake route
  instead (same rule as the rest of this public repo).
- **Refresh-token rotation bug is easy to reintroduce** → if a future
  contributor "simplifies" `strava_authorize.py` by pattern-matching it
  back onto `sync/authorize.py`'s Google shape, the poller silently breaks
  after one refresh cycle. Mitigation: comment at the point of divergence
  explaining why, plus a test asserting the stored token file changes after
  a refresh call.
- **`db.time_col()`'s `key[0]`-is-a-time-column assumption doesn't hold for
  either new table** → silently wrong `MIN`/`MAX` in the backfill
  verification report, and a broken (or crashing) catch-up window if the
  sync poller reuses `window_for()` unmodified. Mitigation: the
  `TableSpec.time_col` field added in Decision 6/`tasks.md` §2 makes the
  time column explicit per table instead of inferring it from upsert-key
  order, with no change to any existing `TABLES` entry's behavior.
- **Running dashboard's HR tile can look precise while resting on sparse
  coverage** (e.g. a week where only one of five runs has heart-rate data,
  because the earlier runs synced from Strava's API before this account's
  Fitbit device was worn, or the two apps' clocks didn't overlap) → the
  tile would silently average just that one run and read as if it
  represented the week. Mitigation: Decision 8 requires showing how many
  runs actually contributed HR data alongside the figure, not just the
  number itself.
- **`strava_activity_hr`'s `LEFT JOIN` against `health.heart_rate` costs a
  range scan per activity on every read** → negligible today (hundreds of
  activities, one indexed scan each), but if this view ever gets queried
  from a context scanning many more activities than a 7d/30d dashboard
  window (e.g. a future all-time Trends-style panel), cost could grow
  linearly with total activity count. Mitigation: not addressed now since
  no such query exists yet; Decision 9 already names the
  materialized-view escape hatch if it's needed later.
- **Track-point duplication if backfill and sync derive `time` differently**
  → a one-second disagreement between the GPX-absolute time and the
  `start_time + offset` time would double every point of an activity ingested
  by both paths, silently violating the idempotency both specs promise.
  Mitigation: the single canonical derivation in Decision 10 (both paths use
  `start_time + round(offset)`), plus sync's load-bearing "skip activities
  already in `activity_track`" guard.
- **Rotating refresh token stranded by a crash mid-refresh** → Strava kills
  the old refresh token the instant it issues a new one, so a crash between
  refresh and persist locks the poller out until a manual re-auth — a genuine
  risk on the Pi's SD card / power loss. Mitigation: Decision 5 requires
  persisting the new token atomically (temp + `os.replace`) *before* the
  access token is used, and a test asserting the file changed after a refresh.
- **Generated `geography` column may not survive compression** on the pinned
  image → unverified interaction between a `GENERATED ... STORED` PostGIS
  column, its GiST index, and native TimescaleDB compression. Mitigation: the
  spike task in Decision 4 / `tasks.md` §2 proves it end-to-end before the
  generated column is relied on, with a plain-column fallback named.

## Migration Plan

1. Land `homelab`'s PostGIS platform-bootstrap change first (or in
   parallel — this repo's standalone mode has no dependency on it).
2. Add migration `infra/migrations/005_strava.sql`: `strava_activity`
   (plain table, columns per the schema spec — including both `moving_time_s`
   and `elapsed_time_s`, `source`, UTC `start_time`), `activity_track`
   (hypertable with explicit `chunk_time_interval` + compression policy,
   `source` column, generated geography), the PostGIS guard per Decision 4,
   and the `strava_activity_hr` view (elapsed-time window per Decision 9).
3. Implement `backfill/strava.py`, wire into `backfill/__main__.py` and
   `backfill/db.py`'s `TABLES` registry; run once against the existing
   export.
4. Implement `sync/strava_authorize.py` (one-time interactive auth,
   rotation-safe refresh) and `sync/strava_poller.py`; verify a full cycle
   end-to-end before enabling the ~6h schedule.
5. Verify row counts / time ranges against the raw export (same
   verification discipline as the Fitbit backfill's table-stats report).
6. Add `infra/grafana/dashboards/health-running.json` per Decision 8; load
   it against the backfilled data and confirm the 7d/30d tiles match a
   hand-computed spot check.

No rollback complexity beyond dropping the two new tables and the
provisioned dashboard file — this change is additive and touches no
existing table or role.

## Open Questions

- **Resolved — activity-type filter field:** the real export has no
  `sport_type` column, so both paths filter on the coarse activity type
  (`activity_type` column, default allowlist `Run,Hike`); see Decision 3.
- **Resolved — poller deployment shape:** a second `strava-sync` Compose
  service reusing the sync image (Decision 11).
- **Generated-geography × compression** on the pinned image is unverified
  (Decision 4) — the one open technical risk; gated by a spike task before
  the generated column is relied on.
