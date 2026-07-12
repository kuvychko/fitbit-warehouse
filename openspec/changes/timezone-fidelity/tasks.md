# Tasks: timezone-fidelity

## 1. Schema (health-schema)

- [ ] 1.1 Migration 003: nullable `utc_offset_s integer` on heart_rate,
      spo2, hrv, azm, weight, body_fat, sleep_session, sleep_stage
      (guarded ADD COLUMN IF NOT EXISTS); new `steps_daily` hypertable
      (day PK, steps, source, device, 1-year chunks)
- [ ] 1.2 Verify: re-run idempotent; ALTERs metadata-only on the compressed
      local tables; apply to warehouse-db

## 2. Poller (health-api-sync)

- [ ] 2.1 Mappers populate `utc_offset_s` from `utcOffset` /
      `startUtcOffset` / per-stage offsets; extend TABLES registry cols
- [ ] 2.2 New dailyRollUp puller → `steps_daily` (day from civilStartTime,
      DO UPDATE, catch-up window like other streams)
- [ ] 2.3 Bootstrap mode: deep dailyRollUp walk 2016-10-01 → now, chunked
      to the (empirically discovered) range cap; idempotent
- [ ] 2.4 **Validation gate**: compare `steps_daily` against the Fitbit app
      for a normal week and a travel week; if all-sources dailyRollUp
      disagrees, switch to google-wearables family and re-verify
- [ ] 2.5 Mapper tests (offsets incl. negative/half-hour zones; civil-day
      extraction)

## 3. Backfill (takeout-backfill)

- [ ] 3.1 Google Fit parser: per-day Basis steps sums → `steps_daily`
      (googlefit-takeout source); run against local + warehouse-db

## 4. Dashboards + deploy (health-dashboards)

- [ ] 4.1 Steps panel → steps_daily (max per source), local-midnight
      rendering via existing $timezone variable
- [ ] 4.2 Deploy: rebuild sync on the Pi; run bootstrap once; verify panel
      totals match the app for spot-checked days; docs updated
      (takeout-format.md provenance section, health-api-notes.md)
