-- 004: analytics layer (openspec change: insight-dashboards)
--   * hourly continuous aggregates over the intraday hypertables — never
--     daily: hour buckets stay timezone-neutral, so dashboards can still
--     regroup into civil days under any $timezone. Real-time reads (the
--     default: materialized_only = false) mean the 2-hourly API poller's
--     newest rows show up immediately, before the next refresh.
--   * heart_rate_hourly stores a percentile_agg() sketch, not raw numbers —
--     sketches are composable (rollup() across hours -> true daily/weekly
--     percentiles via approx_percentile()); plain percentile_cont() numbers
--     are not. Requires timescaledb_toolkit.
--   * daily_baseline / sleep_composition are plain views (daily-grain source
--     data is cheap at any zoom) — the one shared "trailing 30-day median"
--     definition every dashboard reads, so it can't drift between them.
-- Idempotent: CREATE MATERIALIZED VIEW IF NOT EXISTS + policies with
-- if_not_exists => TRUE; views are CREATE OR REPLACE; grants are safe to
-- repeat.

\set ON_ERROR_STOP on

-- Toolkit guard -----------------------------------------------------------
-- Shared-mode clusters get timescaledb_toolkit from the platform bootstrap
-- (warehouse cluster contract); standalone mode's migrate service connects
-- as a superuser and can create it here. Either way, fail fast with a clear
-- message rather than a cryptic "function percentile_agg does not exist".
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit;
EXCEPTION WHEN insufficient_privilege THEN
    NULL; -- shared-mode role without CREATE on the database; checked below
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_extension WHERE extname = 'timescaledb_toolkit') THEN
        RAISE EXCEPTION 'timescaledb_toolkit extension is not installed. '
            'Shared-mode: ask the platform admin to run '
            '"CREATE EXTENSION timescaledb_toolkit" in the warehouse database '
            '(see the warehouse cluster contract). Standalone: the migrate '
            'service must connect as a role with CREATE privilege on the '
            'database (default: postgres).';
    END IF;
END
$$;

SET ROLE health_owner;

-- Hourly continuous aggregates ------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS health.heart_rate_hourly
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    time_bucket('1 hour', time) AS hour,
    device,
    source,
    percentile_agg(bpm) AS bpm_agg,
    count(*)             AS samples
FROM health.heart_rate
GROUP BY 1, 2, 3
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS health.steps_hourly
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    time_bucket('1 hour', time) AS hour,
    device,
    source,
    sum(steps) AS steps,
    count(*)   AS samples
FROM health.steps
GROUP BY 1, 2, 3
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS health.calories_hourly
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    time_bucket('1 hour', time) AS hour,
    device,
    source,
    sum(kcal) AS kcal,
    count(*)  AS samples
FROM health.calories
GROUP BY 1, 2, 3
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS health.azm_hourly
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    time_bucket('1 hour', time) AS hour,
    device,
    source,
    zone,
    sum(minutes) AS minutes
FROM health.azm
GROUP BY 1, 2, 3, 4
WITH NO DATA;

-- Refresh policies --------------------------------------------------------
-- start_offset 31 days: covers the sync poller's CATCHUP_CAP_DAYS=30 window
-- (a catch-up run can write rows up to 30 days in the past) with a 1-day
-- margin, so late-arriving catch-up data always gets re-materialized —
-- comfortably inside the 90-day compression boundary on the source
-- hypertables (002_hypertables.sql). end_offset 1 hour keeps the
-- currently-in-progress hour out of the materialized region (real-time
-- reads already cover it live).
SELECT add_continuous_aggregate_policy('health.heart_rate_hourly',
    start_offset => INTERVAL '31 days', end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 day', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('health.steps_hourly',
    start_offset => INTERVAL '31 days', end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 day', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('health.calories_hourly',
    start_offset => INTERVAL '31 days', end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 day', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('health.azm_hourly',
    start_offset => INTERVAL '31 days', end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 day', if_not_exists => TRUE);

-- Sleep composition ---------------------------------------------------------
-- Unpacks levels_summary (Fitbit's levels.summary jsonb) into one row per
-- night per stage. Same {key: {minutes: N, ...}} shape for both vocabularies
-- (stages: wake/light/deep/rem; classic: awake/restless/asleep), so no
-- vocabulary branching is needed.
CREATE OR REPLACE VIEW health.sleep_composition AS
SELECT
    ss.day,
    ss.start_time,
    ss.log_id,
    ss.sleep_type,
    ss.source,
    stage.key                          AS level,
    (stage.value ->> 'minutes')::integer AS minutes
FROM health.sleep_session ss
CROSS JOIN LATERAL jsonb_each(ss.levels_summary) AS stage(key, value)
WHERE ss.levels_summary IS NOT NULL;

-- Daily baseline --------------------------------------------------------
-- health.daily_metric: every baseline-eligible metric normalized to
-- (day, metric, value). Resting HR / HRV / steps are already one
-- Fitbit-computed value per day. Sleep score and the nightly-summary tables
-- (breathing rate, nightly skin temperature) don't carry their own day
-- column, so they're joined through sleep_session for its Fitbit-civil
-- dateOfSleep — falling back to a UTC date cast only if no matching session
-- row exists (rare: a summary row without its parent session).
-- nightly_temperature.avg_c is an ABSOLUTE nightly skin temperature
-- (~31-36 C), not a pre-computed deviation (that's device_temperature,
-- a separate per-minute table); daily_baseline's value-minus-median still
-- yields a genuine deviation from the person's own 30-day baseline, which
-- is the only thing that matters for the morning-report use case.
CREATE OR REPLACE VIEW health.daily_metric AS
SELECT day, 'resting_hr'::text AS metric, bpm::double precision AS value
FROM health.resting_heart_rate
UNION ALL
SELECT day, 'hrv_rmssd', rmssd_ms::double precision
FROM health.hrv_daily
WHERE rmssd_ms IS NOT NULL
UNION ALL
SELECT day, 'steps', steps::double precision
FROM health.steps_daily
UNION ALL
SELECT ss.day, 'sleep_minutes', ss.minutes_asleep::double precision
FROM health.sleep_session ss
WHERE ss.main_sleep AND ss.minutes_asleep IS NOT NULL
UNION ALL
SELECT ss.day, 'sleep_score', sc.overall::double precision
FROM health.sleep_score sc
JOIN health.sleep_session ss ON ss.log_id = sc.sleep_log_id
WHERE sc.overall IS NOT NULL
UNION ALL
-- breathing_rate.sleep_end is rounded coarser than sleep_session.end_time
-- (observed offsets up to a few minutes, never exact), so the exact-equality
-- join below only matches ~4% of rows; the other 96% fall through to the UTC
-- date cast. That sounds worse than it is: every sample checked against
-- production had the UTC-cast day agree with sleep_session's Fitbit-civil
-- day anyway (sleep_end isn't near a UTC midnight boundary for any
-- timezone/schedule combination observed). A closest-match window join was
-- tried instead and reliably attributed the correct day, but LEFT JOIN
-- LATERAL here made health.daily_baseline's own correlated 30-day lookup
-- (itself already O(rows x window) against this view) blow up to a
-- multi-hundred-million-cost plan and time out in production. Reverted:
-- correctness here is a paper win over the existing fallback, and not worth
-- another expensive join re-evaluated inside an already-correlated view.
SELECT COALESCE(ss.day, (br.sleep_end AT TIME ZONE 'UTC')::date),
       'breathing_rate', br.full_bpm::double precision
FROM health.breathing_rate br
LEFT JOIN health.sleep_session ss ON ss.end_time = br.sleep_end
WHERE br.full_bpm IS NOT NULL
UNION ALL
SELECT COALESCE(ss.day, (nt.sleep_start AT TIME ZONE 'UTC')::date),
       'nightly_temp', nt.avg_c::double precision
FROM health.nightly_temperature nt
LEFT JOIN health.sleep_session ss ON ss.start_time = nt.sleep_start
WHERE nt.avg_c IS NOT NULL;

-- health.daily_baseline: each metric's value alongside the median/p25/p75
-- of that same metric over the 30 days strictly before it (today never
-- influences its own baseline). percentile_cont() isn't a window function
-- in Postgres, hence the LATERAL correlated subquery.
CREATE OR REPLACE VIEW health.daily_baseline AS
SELECT
    dm.day,
    dm.metric,
    dm.value,
    baseline.median,
    baseline.p25,
    baseline.p75
FROM health.daily_metric dm
CROSS JOIN LATERAL (
    SELECT
        percentile_cont(0.5)  WITHIN GROUP (ORDER BY prior.value) AS median,
        percentile_cont(0.25) WITHIN GROUP (ORDER BY prior.value) AS p25,
        percentile_cont(0.75) WITHIN GROUP (ORDER BY prior.value) AS p75
    FROM health.daily_metric prior
    WHERE prior.metric = dm.metric
      AND prior.day >= dm.day - 30
      AND prior.day < dm.day
) baseline;

-- Grants ------------------------------------------------------------------
GRANT SELECT ON health.heart_rate_hourly, health.steps_hourly,
    health.calories_hourly, health.azm_hourly,
    health.sleep_composition, health.daily_metric, health.daily_baseline
    TO health_ro;

RESET ROLE;
