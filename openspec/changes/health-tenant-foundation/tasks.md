# Tasks: health-tenant-foundation

## 0. External kickoffs (slow — start immediately)

- [x] 0.1 Request the Google Takeout Fitbit export (user action; generation can
      take hours–days; gates 2.x) — done 2026-07-11: Fitbit (482 MB) + Google
      Fit (11 MB, Basis Peak) zips in `data/`, verified complete
- [x] 0.2 Create the Google Cloud project + OAuth 2.0 client in testing mode
      (user action with guided steps; gates 3.1) — done 2026-07-12; desktop
      client + 3 readonly restricted scopes

## 1. Database foundation (health-schema)

- [x] 1.1 `infra/docker-compose.yml`: two-mode layout — `db` (TimescaleDB,
      pinned image, `profiles: [standalone]`), one-shot `migrate` service,
      networks for both modes; `.env.example` finalized against it (+ root
      `compose.yaml` include so the root `.env` is picked up)
- [x] 1.2 Migration 001: guarded roles (`health_owner/_rw/_ro`), guarded
      `CREATE SCHEMA health`, grants + default privileges,
      `search_path = health, public`
- [x] 1.3 Migration 002: metric hypertables (heart_rate intraday, resting HR,
      sleep sessions + stages + scores, steps, calories/distance/activity
      levels, spo2, hrv, breathing_rate, skin temperature, azm, weight/body
      fat), natural-key unique constraints, `source` + `device` provenance
      columns, compression on intraday tables
- [x] 1.4 Verify: migrations idempotent (run twice), `health_rw`/`health_ro`
      privilege matrix, hypertables confirmed — in standalone mode

## 2. Takeout backfill (takeout-backfill)

- [x] 2.1 Inventory the actual export: map directories/files → metric families;
      record the mapping (incl. CSV-vs-JSON duality and the classic-JSON UTC
      quirk) in `docs/takeout-format.md`
- [x] 2.2 `backfill/` Python CLI: parsers per metric family (CSV-first, JSON
      only where no CSV exists) → normalized rows → batched upsert; per-file
      validation; loud skip reporting; summary with row counts + MIN/MAX(time)
- [x] 2.2b Google Fit parser: Basis Peak raw streams (Data-Points JSON, ns
      epochs) → same tables, `source='googlefit-takeout'`,
      `device='Basis Peak'`; skip derived/merged and phone-sensor streams
      (+ Fit/All Sessions sleep sessions discovered during verification)
- [x] 2.3 Load full history (shared-cluster mode against `warehouse-db`);
      verify counts/ranges against raw files; re-run → identical counts
      — verified locally AND on warehouse-db 2026-07-11: 48M rows, 19
      tables, totals identical across environments and across re-runs
- [x] 2.4 Synthetic-fixture tests for parsers (no real health data in repo)
      — tests/test_parsers.py, 15 tests

## 3. Google Health API sync (health-api-sync)

- [x] 3.1 **Spike (gates the rest of phase 3)**: interactive OAuth flow in
      testing mode, refresh token stored at `GOOGLE_TOKEN_PATH`, one successful
      data-type read; document findings (scopes, quotas, data-type shapes) in
      `docs/health-api-notes.md` — done 2026-07-12: sync/authorize.py; live
      200s on heart-rate list + steps dailyRollUp; 7-day testing-mode token
      expiry is the open risk (recheck ~07-19; escape hatch documented)
- [x] 3.2 `sync/` poller: thin API client, per-data-type pullers, catch-up
      window (default 7d), natural-key upserts (DO UPDATE for daily summaries,
      DO NOTHING for immutable intraday), 429/Retry-After handling
      — done 2026-07-12: DB-driven windows (last point - 24h overlap, 30d
      cap), 13 data types (list/rollup/daily strategies), COALESCE upserts;
      verified locally: seam continuous, second cycle writes 0 intraday rows
- [ ] 3.3 Containerize + schedule (compose service on the Pi, cron-style);
      Healthchecks success/fail pings with unset-URL no-op
- [ ] 3.4 Deploy to the Pi in shared-cluster mode; smoke: yesterday's data
      lands; overlap re-poll adds no rows; kill poller → dead-man alert fires

## 4. Dashboards + wrap-up (health-dashboards)

- [x] 4.1 Grafana provisioning: `health_ro` datasource + starter dashboard
      (HR, sleep, steps) spanning the backfill↔sync seam — verified live:
      datasource healthy, 2015 Basis + 2026 Fitbit data render, INSERT denied
- [ ] 4.2 Verify seam: panels continuous across the boundary date; datasource
      role cannot INSERT
- [ ] 4.3 README pass: quickstart proven from a fresh clone (standalone mode),
      screenshots reviewed for personal-data leakage before commit
