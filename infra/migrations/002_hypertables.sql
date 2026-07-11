-- 002: metric hypertables (tier-1 families confirmed in the Takeout exports).
-- Idempotent: CREATE TABLE IF NOT EXISTS + create_hypertable(if_not_exists),
-- compression enablement guarded against timescaledb_information.
--
-- Conventions (see openspec design):
--   * every time column is timestamptz stored as UTC (parsers normalize);
--     daily-grain tables key on a plain date
--   * natural keys EXCLUDE provenance, so takeout/API overlap upserts cleanly
--   * source: 'fitbit-takeout' | 'googlefit-takeout' | 'api'
--   * device: recording device when the data carries it ("Charge 5",
--     "Basis Peak"), else NULL
--   * units are SI-ish: kcal, meters, celsius, kg; parsers convert

\set ON_ERROR_STOP on

SET ROLE health_owner;

-- Intraday samples ------------------------------------------------------------

-- heart_rate_YYYY-MM-DD.csv (~5-15 s grain) / Basis Peak (per-minute)
CREATE TABLE IF NOT EXISTS health.heart_rate (
    time        timestamptz NOT NULL,
    bpm         real        NOT NULL,
    confidence  smallint,
    source      text        NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device      text,
    PRIMARY KEY (time)
);
SELECT create_hypertable('health.heart_rate', 'time', if_not_exists => TRUE);

-- steps_YYYY-MM-DD.csv (per-minute) / Basis Peak
CREATE TABLE IF NOT EXISTS health.steps (
    time    timestamptz NOT NULL,
    steps   integer     NOT NULL,
    source  text        NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device  text,
    PRIMARY KEY (time)
);
SELECT create_hypertable('health.steps', 'time', if_not_exists => TRUE);

-- calories_YYYY-MM-DD.csv (per-minute) / Basis Peak
CREATE TABLE IF NOT EXISTS health.calories (
    time    timestamptz NOT NULL,
    kcal    real        NOT NULL,
    source  text        NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device  text,
    PRIMARY KEY (time)
);
SELECT create_hypertable('health.calories', 'time', if_not_exists => TRUE);

-- distance_YYYY-MM-DD.csv (per-minute, meters)
CREATE TABLE IF NOT EXISTS health.distance (
    time    timestamptz NOT NULL,
    meters  real        NOT NULL,
    source  text        NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device  text,
    PRIMARY KEY (time)
);
SELECT create_hypertable('health.distance', 'time', if_not_exists => TRUE);

-- floors_YYYY-MM-DD.csv (per-minute)
CREATE TABLE IF NOT EXISTS health.floors (
    time    timestamptz NOT NULL,
    floors  smallint    NOT NULL,
    source  text        NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device  text,
    PRIMARY KEY (time)
);
SELECT create_hypertable('health.floors', 'time', if_not_exists => TRUE);

-- "Minute SpO2 - YYYY-MM-DD.csv" / oxygen_saturation_YYYY-MM-DD.csv
CREATE TABLE IF NOT EXISTS health.spo2 (
    time    timestamptz NOT NULL,
    pct     real        NOT NULL,
    source  text        NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device  text,
    PRIMARY KEY (time)
);
SELECT create_hypertable('health.spo2', 'time', if_not_exists => TRUE);

-- heart_rate_variability_YYYY-MM-DD.csv (5-minute rmssd/sdrr during sleep)
CREATE TABLE IF NOT EXISTS health.hrv (
    time      timestamptz NOT NULL,
    rmssd_ms  real        NOT NULL,
    sdrr_ms   real,
    source    text        NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device    text,
    PRIMARY KEY (time)
);
SELECT create_hypertable('health.hrv', 'time', if_not_exists => TRUE);

-- "Device Temperature - YYYY-MM-DD.csv" (per-minute, deviation from baseline)
CREATE TABLE IF NOT EXISTS health.device_temperature (
    time         timestamptz NOT NULL,
    deviation_c  real        NOT NULL,
    sensor_type  text,
    source       text        NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device       text,
    PRIMARY KEY (time)
);
SELECT create_hypertable('health.device_temperature', 'time', if_not_exists => TRUE);

-- "Active Zone Minutes - YYYY-MM-DD.csv" (per-minute, zone FAT_BURN/CARDIO/PEAK)
CREATE TABLE IF NOT EXISTS health.azm (
    time     timestamptz NOT NULL,
    zone     text        NOT NULL,
    minutes  smallint    NOT NULL,
    source   text        NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device   text,
    PRIMARY KEY (time, zone)
);
SELECT create_hypertable('health.azm', 'time', if_not_exists => TRUE);

-- Sleep -------------------------------------------------------------------

-- sleep-YYYY-MM-DD.json (classic JSON only; one row per sleep log)
CREATE TABLE IF NOT EXISTS health.sleep_session (
    start_time              timestamptz NOT NULL,
    end_time                timestamptz NOT NULL,
    day                     date        NOT NULL,   -- Fitbit dateOfSleep
    log_id                  bigint,
    duration_ms             bigint,
    minutes_asleep          smallint,
    minutes_awake           smallint,
    minutes_to_fall_asleep  smallint,
    minutes_after_wakeup    smallint,
    time_in_bed             smallint,
    efficiency              smallint,
    sleep_type              text,                   -- 'stages' | 'classic'
    log_type                text,
    main_sleep              boolean,
    levels_summary          jsonb,
    source                  text NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device                  text,
    PRIMARY KEY (start_time)
);
SELECT create_hypertable('health.sleep_session', 'start_time',
                         chunk_time_interval => INTERVAL '1 month',
                         if_not_exists => TRUE);

-- sleep JSON levels.data / levels.shortData segments
CREATE TABLE IF NOT EXISTS health.sleep_stage (
    time      timestamptz NOT NULL,
    level     text        NOT NULL,  -- wake/light/deep/rem (stages) or awake/restless/asleep (classic)
    seconds   integer     NOT NULL,
    is_short  boolean     NOT NULL DEFAULT false,  -- from levels.shortData (overlaps main segments)
    source    text        NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    PRIMARY KEY (time, level)
);
SELECT create_hypertable('health.sleep_stage', 'time', if_not_exists => TRUE);

-- Sleep Score/sleep_score.csv (one row per sleep log)
CREATE TABLE IF NOT EXISTS health.sleep_score (
    time            timestamptz NOT NULL,
    sleep_log_id    bigint,
    overall         smallint,
    composition     smallint,
    revitalization  smallint,
    duration_score  smallint,
    deep_sleep_min  smallint,
    resting_hr      real,
    restlessness    real,
    source          text NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    PRIMARY KEY (time)
);
SELECT create_hypertable('health.sleep_score', 'time',
                         chunk_time_interval => INTERVAL '1 month',
                         if_not_exists => TRUE);

-- Per-sleep summaries ---------------------------------------------------------

-- respiratory_rate_sleep_summary_YYYY-MM-DD.csv (keyed by sleep end; breaths/min)
CREATE TABLE IF NOT EXISTS health.breathing_rate (
    sleep_end  timestamptz NOT NULL,
    deep_bpm   real,
    light_bpm  real,
    rem_bpm    real,
    full_bpm   real,
    source     text NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device     text,
    PRIMARY KEY (sleep_end)
);
SELECT create_hypertable('health.breathing_rate', 'sleep_end',
                         chunk_time_interval => INTERVAL '1 month',
                         if_not_exists => TRUE);

-- "Computed Temperature - YYYY-MM-DD.csv" (nightly skin-temperature summary)
CREATE TABLE IF NOT EXISTS health.nightly_temperature (
    sleep_start  timestamptz NOT NULL,
    sleep_end    timestamptz,
    avg_c        real,
    samples      integer,
    stdev_c      real,
    source       text NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device       text,
    PRIMARY KEY (sleep_start)
);
SELECT create_hypertable('health.nightly_temperature', 'sleep_start',
                         chunk_time_interval => INTERVAL '1 month',
                         if_not_exists => TRUE);

-- Daily summaries ---------------------------------------------------------

-- resting_heart_rate-*.json (daily; value + error)
CREATE TABLE IF NOT EXISTS health.resting_heart_rate (
    day        date NOT NULL,
    bpm        real NOT NULL,
    error_bpm  real,
    source     text NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    PRIMARY KEY (day)
);
SELECT create_hypertable('health.resting_heart_rate', 'day',
                         chunk_time_interval => INTERVAL '1 year',
                         if_not_exists => TRUE);

-- sedentary/lightly/moderately/very_active_minutes-*.json (daily totals)
CREATE TABLE IF NOT EXISTS health.active_minutes_daily (
    day             date NOT NULL,
    sedentary_min   smallint,
    lightly_min     smallint,
    moderately_min  smallint,
    very_min        smallint,
    source          text NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    PRIMARY KEY (day)
);
SELECT create_hypertable('health.active_minutes_daily', 'day',
                         chunk_time_interval => INTERVAL '1 year',
                         if_not_exists => TRUE);

-- "Daily Heart Rate Variability Summary - *.csv" (daily rmssd/nremhr/entropy)
CREATE TABLE IF NOT EXISTS health.hrv_daily (
    day         date NOT NULL,
    rmssd_ms    real,
    nremhr_bpm  real,
    entropy     real,
    source      text NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    PRIMARY KEY (day)
);
SELECT create_hypertable('health.hrv_daily', 'day',
                         chunk_time_interval => INTERVAL '1 year',
                         if_not_exists => TRUE);

-- Body ----------------------------------------------------------------------

-- weight-*.json (sparse; lbs+BMI in export, stored as kg)
CREATE TABLE IF NOT EXISTS health.weight (
    time       timestamptz NOT NULL,
    weight_kg  real        NOT NULL,
    bmi        real,
    source     text NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device     text,
    PRIMARY KEY (time)
);
SELECT create_hypertable('health.weight', 'time',
                         chunk_time_interval => INTERVAL '1 year',
                         if_not_exists => TRUE);

-- body_fat_YYYY-MM-DD.csv (sparse)
CREATE TABLE IF NOT EXISTS health.body_fat (
    time     timestamptz NOT NULL,
    fat_pct  real        NOT NULL,
    source   text NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device   text,
    PRIMARY KEY (time)
);
SELECT create_hypertable('health.body_fat', 'time',
                         chunk_time_interval => INTERVAL '1 year',
                         if_not_exists => TRUE);

-- Compression on the high-volume intraday tables ------------------------------
-- Guarded: only enable + schedule where not already enabled, so re-runs no-op.
DO $$
DECLARE
    t text;
    orderby text;
BEGIN
    FOR t IN
        SELECT hypertable_name
        FROM timescaledb_information.hypertables
        WHERE hypertable_schema = 'health'
          AND NOT compression_enabled
          AND hypertable_name IN ('heart_rate', 'steps', 'calories', 'distance',
                                  'floors', 'spo2', 'hrv', 'device_temperature',
                                  'azm', 'sleep_stage')
    LOOP
        -- the full natural key must appear in the compression key
        orderby := CASE t
                       WHEN 'azm'         THEN 'time, zone'
                       WHEN 'sleep_stage' THEN 'time, level'
                       ELSE 'time'
                   END;
        EXECUTE format(
            'ALTER TABLE health.%I SET (timescaledb.compress, '
            'timescaledb.compress_segmentby = ''source'', '
            'timescaledb.compress_orderby = %L)', t, orderby);
        -- 90 days: far outside the API poller's catch-up window, so upserts
        -- never target compressed chunks.
        PERFORM add_compression_policy(format('health.%I', t)::regclass,
                                       compress_after => INTERVAL '90 days',
                                       if_not_exists => TRUE);
    END LOOP;
END
$$;

RESET ROLE;
