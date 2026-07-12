-- 003: timezone fidelity (openspec change: timezone-fidelity)
--   * utc_offset_s on list-fed tables — the local-time offset (seconds) a
--     measurement was experienced at, from the Health API's per-sample
--     utcOffset. NULL = unknown / assume home zone (all pre-existing rows,
--     Takeout CSVs, rollup-fed rows). Offset changes mark travel.
--   * steps_daily — Fitbit-civil-day totals (travel-aware, deduplicated by
--     Fitbit), fed by the API dailyRollUp + Basis-era derivation.
-- Idempotent: ADD COLUMN IF NOT EXISTS is metadata-only (nullable, no
-- default) and safe on compressed chunks.

\set ON_ERROR_STOP on

SET ROLE health_owner;

ALTER TABLE health.heart_rate    ADD COLUMN IF NOT EXISTS utc_offset_s integer;
ALTER TABLE health.spo2          ADD COLUMN IF NOT EXISTS utc_offset_s integer;
ALTER TABLE health.hrv           ADD COLUMN IF NOT EXISTS utc_offset_s integer;
ALTER TABLE health.azm           ADD COLUMN IF NOT EXISTS utc_offset_s integer;
ALTER TABLE health.weight        ADD COLUMN IF NOT EXISTS utc_offset_s integer;
ALTER TABLE health.body_fat      ADD COLUMN IF NOT EXISTS utc_offset_s integer;
ALTER TABLE health.sleep_session ADD COLUMN IF NOT EXISTS utc_offset_s integer;
ALTER TABLE health.sleep_stage   ADD COLUMN IF NOT EXISTS utc_offset_s integer;

-- Fitbit-computed daily step totals keyed by the civil date the user lived.
CREATE TABLE IF NOT EXISTS health.steps_daily (
    day     date    NOT NULL,
    steps   integer NOT NULL,
    source  text    NOT NULL CHECK (source IN ('fitbit-takeout', 'googlefit-takeout', 'api')),
    device  text,
    PRIMARY KEY (day)
);
SELECT create_hypertable('health.steps_daily', 'day',
                         chunk_time_interval => INTERVAL '1 year',
                         if_not_exists => TRUE);

RESET ROLE;
