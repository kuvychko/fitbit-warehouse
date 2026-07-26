-- 005: Strava activities (openspec change: add-strava-activities)
--   * strava_activity: one row per activity, keyed on the immutable Strava
--     activity ID. A PLAIN table (not a hypertable): its natural key isn't a
--     time column, and TimescaleDB requires the partition column in every
--     unique/primary key. Small, slow-growing catalog — dimension-like.
--   * activity_track: one row per GPS fix; a hypertable on `time`. `time` is
--     derived (activity start + point offset) at write time so it's a normal
--     timestamptz column like every other hypertable here. Carries a generated
--     geography(Point,4326) column + GiST index for future geo work.
--   * strava_activity_hr: a read-side VIEW correlating heart_rate over each
--     activity's ELAPSED window — no write-time coupling to Fitbit ingestion.
-- PostGIS is required for the geography column; guarded exactly like 004's
-- timescaledb_toolkit (the platform installs it in shared mode; a standalone
-- superuser creates it here). NB: whether native compression coexists with a
-- GENERATED STORED geography column + GiST index on the pinned image is
-- verified by the tasks.md §2 spike before this migration is trusted in prod.
-- Idempotent: CREATE ... IF NOT EXISTS; create_hypertable(if_not_exists);
-- compression guarded against timescaledb_information; view is CREATE OR
-- REPLACE; grants are safe to repeat.

\set ON_ERROR_STOP on

-- PostGIS guard (mirrors 004_analytics.sql's timescaledb_toolkit guard) -------
-- Mode-agnostic: standalone's superuser role creates the extension in the
-- first block; a shared-mode tenant role lacking CREATE hits
-- insufficient_privilege (swallowed), then the second block fails loud if the
-- platform has not installed postgis yet.
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
        RAISE EXCEPTION 'postgis extension is not installed. '
            'Shared-mode: ask the platform admin to run '
            '"CREATE EXTENSION postgis" in the warehouse database '
            '(see the warehouse cluster contract). Standalone: the migrate '
            'service must connect as a role with CREATE privilege on the '
            'database (default: postgres).';
    END IF;
END
$$;

SET ROLE health_owner;

-- Activity summaries (plain table; natural key = Strava activity ID) ----------
-- start_time is UTC (parsers normalize; CSV "Activity Date" is UTC, verified
-- against the GPX metadata start). Both moving_time_s and elapsed_time_s are
-- stored: pace is defined on moving time, the HR correlation window on elapsed.
-- activity_type is the COARSE Strava type (Run/Hike/...); the bulk export has
-- no granular sport_type, so it is not stored (see design Decision 3).
CREATE TABLE IF NOT EXISTS health.strava_activity (
    activity_id     bigint      NOT NULL,
    start_time      timestamptz NOT NULL,
    utc_offset_s    integer,                        -- local offset when known (API); NULL for backfill
    activity_type   text        NOT NULL,
    name            text,
    distance_m      double precision,
    moving_time_s   integer,
    elapsed_time_s  integer,
    elev_gain_m     real,
    elev_loss_m     real,
    source          text        NOT NULL CHECK (source IN ('strava-export', 'strava-api')),
    PRIMARY KEY (activity_id)
);

-- GPS track points (hypertable on derived `time`) -----------------------------
-- geog is GENERATED from lon/lat (ST_MakePoint takes x=lon, y=lat), SRID 4326
-- = WGS84 (native to GPS, no reprojection). Not inserted by parsers.
CREATE TABLE IF NOT EXISTS health.activity_track (
    activity_id  bigint      NOT NULL,
    time         timestamptz NOT NULL,
    lat          double precision,
    lon          double precision,
    elevation_m  double precision,
    source       text        CHECK (source IN ('strava-export', 'strava-api')),
    geog         geography(Point, 4326)
                 GENERATED ALWAYS AS (
                     ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography
                 ) STORED,
    PRIMARY KEY (activity_id, time)
);
-- Explicit 1-month chunks (not the 7-day default): years of runs at a few per
-- week would otherwise scatter across hundreds of near-empty chunks.
SELECT create_hypertable('health.activity_track', 'time',
                         chunk_time_interval => INTERVAL '1 month',
                         if_not_exists => TRUE);
-- GiST index for geo queries (bounding-box / distance / nearest).
CREATE INDEX IF NOT EXISTS activity_track_geog_idx
    ON health.activity_track USING gist (geog);

-- Compression on the track hypertable (same shape as the intraday tables) -----
-- 90 days: far outside the sync catch-up window, so upserts never target a
-- compressed chunk. No segmentby (activity_id is too high-cardinality); order
-- by (activity_id, time) so a single run's points sit together.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM timescaledb_information.hypertables
        WHERE hypertable_schema = 'health'
          AND hypertable_name = 'activity_track'
          AND compression_enabled
    ) THEN
        ALTER TABLE health.activity_track SET (
            timescaledb.compress,
            timescaledb.compress_orderby = 'activity_id, time'
        );
        PERFORM add_compression_policy('health.activity_track'::regclass,
                                       compress_after => INTERVAL '90 days',
                                       if_not_exists => TRUE);
    END IF;
END
$$;

-- Per-activity heart-rate correlation (read-side view) ------------------------
-- No write-time coupling: neither backfill nor the poller reads health.heart_rate.
-- ELAPSED window, not moving: HR is recorded on wall-clock time through pauses,
-- so a moving-time window would end early and bias the average toward the
-- run's warm-up. LEFT JOIN so every activity has a row; count(bpm) = 0 and the
-- bpm aggregates are NULL when no samples fall in the window. Rolls HR up
-- across every device/source with no per-device dedup (an average, unlike a
-- sum, isn't distorted by consistent overlapping readings — same precedent as
-- health-trends.json's heart-rate panels).
CREATE OR REPLACE VIEW health.strava_activity_hr AS
SELECT
    a.activity_id,
    count(hr.bpm)                            AS hr_sample_count,
    sum(hr.bpm)                              AS hr_bpm_sum,
    avg(hr.bpm)                              AS hr_bpm_avg,
    min(hr.bpm)                              AS hr_bpm_min,
    max(hr.bpm)                              AS hr_bpm_max
FROM health.strava_activity a
LEFT JOIN health.heart_rate hr
       ON hr.time >= a.start_time
      AND hr.time <  a.start_time + make_interval(secs => a.elapsed_time_s)
GROUP BY a.activity_id;

-- Grants ----------------------------------------------------------------------
-- ALTER DEFAULT PRIVILEGES (migration 001) already covers the two tables (owned
-- by health_owner). The view is granted explicitly, consistent with 004.
GRANT SELECT ON health.strava_activity_hr TO health_ro;

RESET ROLE;
