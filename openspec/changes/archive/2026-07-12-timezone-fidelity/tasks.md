# Tasks: timezone-fidelity

## 1. Schema (health-schema)

- [x] 1.1 Migration 003: nullable `utc_offset_s integer` on heart_rate,
      spo2, hrv, azm, weight, body_fat, sleep_session, sleep_stage
      (guarded ADD COLUMN IF NOT EXISTS); new `steps_daily` hypertable
      (day PK, steps, source, device, 1-year chunks)
- [x] 1.2 Verify: re-run idempotent; ALTERs metadata-only on the compressed
      local tables; apply to warehouse-db

## 2. Poller (health-api-sync)

- [x] 2.1 Mappers populate `utc_offset_s` from `utcOffset` /
      `startUtcOffset` / per-stage offsets; extend TABLES registry cols
- [x] 2.2 New dailyRollUp puller → `steps_daily` (day from civilStartTime,
      DO UPDATE, catch-up window like other streams)
- [x] 2.3 Bootstrap mode: deep dailyRollUp walk 2016-10-01 → now, chunked
      to the (empirically discovered) range cap; idempotent
- [x] 2.4 **Validation gate**: compare `steps_daily` against the Fitbit app
      for a normal week and a travel week; if all-sources dailyRollUp
      disagrees, switch to google-wearables family and re-verify
      — dailyRollUp IS deduplicated (07-04: 13,165 = wearable number, not
      33,715 naive sum; ~0.6% under app display, accepted); travel-week
      spot-check deferred to the next actual trip
- [x] 2.5 Mapper tests (offsets incl. negative/half-hour zones; civil-day
      extraction)

## 3. Backfill (takeout-backfill)

- [x] 3.1 Google Fit parser: per-day Basis steps sums → `steps_daily`
      (googlefit-takeout source); run against local + warehouse-db

## 4. Dashboards + deploy (health-dashboards)

- [x] 4.1 Steps panel → steps_daily (max per source), local-midnight
      rendering via existing $timezone variable
- [x] 4.2 Deploy: rebuild sync on the Pi; run bootstrap once; verify panel
      totals match the app for spot-checked days; docs updated
      (takeout-format.md provenance section, health-api-notes.md)
      — done 2026-07-12: Pi cycle OK with steps@daily; utc_offset_s=-25200
      arriving from the Pi poller; steps_daily 2,601 days on warehouse-db;
      anchor day 13,165 verified against app
