# Proposal: timezone-fidelity

## Why

The warehouse stores every measurement at its correct UTC instant, but two
timezone-related gaps remain after `health-tenant-foundation`:

1. **We discard data the API hands us.** Every list-based Health API sample
   carries a `utcOffset` — the local time the measurement was *experienced
   at*. Beyond presentation, offset changes are a high-value signal in their
   own right: they mark travel, which correlates with sleep disruption, HRV
   dips, and activity changes. Once discarded it is unrecoverable.
2. **Day-grain panels are home-zone-anchored.** Intraday sums bucket days at
   a fixed dashboard timezone, so during travel the steps-per-day panel
   splits local days across two buckets and disagrees both with the Fitbit
   app and with the Fitbit-computed dailies (resting HR, daily HRV, sleep
   day) that are already travel-aware.

## What Changes

- **Keep the offset**: additive nullable `utc_offset_s` column on the
  list-fed metric tables (heart_rate, spo2, hrv, azm, weight, body_fat,
  sleep_session, sleep_stage); the poller populates it from the API's
  per-sample `utcOffset`. Backfill/rollup rows leave it NULL (Takeout CSVs
  and rollup responses carry no offset).
- **Fitbit-civil-day dailies**: new `steps_daily` table (day, steps, source)
  fed by the API's `dailyRollUp` (civil-date keyed, Fitbit-deduplicated
  across devices). History comes from a one-time deep dailyRollUp pull
  (2016→present; the Takeout export carries no Fitbit-computed daily step
  totals). The 2015 Basis era is derived by summing Basis intraday steps per
  `TAKEOUT_TZ` day (single device — no dedup hazard). The steps-per-day
  panel switches to this table, making trip weeks match the app.
- Poller cursor/window logic extends to the new daily puller; docs updated
  (timezone policy section in `docs/takeout-format.md` /
  `docs/health-api-notes.md`).

## Capabilities

### New Capabilities
_None — this extends existing capabilities._

### Modified Capabilities
- `health-schema`: new nullable `utc_offset_s` column on list-fed metric
  tables; new `steps_daily` hypertable (day-grain natural key).
- `health-api-sync`: poller stores per-sample UTC offsets; new dailyRollUp
  puller feeding `steps_daily`.
- `takeout-backfill`: Basis-era `steps_daily` derived from Basis intraday
  sums (Fitbit-era history is NOT derivable from the export's intraday CSVs
  without re-creating the multi-device dedup problem — it comes from the
  API's deep dailyRollUp instead).
- `health-dashboards`: steps-per-day panel reads `steps_daily`; travel
  periods render as the user lived them.

## Impact

- Migration 003 (additive; nullable-column ALTERs are metadata-only even on
  compressed hypertables).
- `sync/poller.py` mappers + one new puller; `backfill/` one new parser
  mapping; dashboard JSON panel swap.
- No breaking changes; existing rows keep `utc_offset_s = NULL` ("unknown,
  assume home zone").
