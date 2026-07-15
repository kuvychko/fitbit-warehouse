## Why

The warehouse holds a decade of intraday health data, but the only dashboard is a
90-day overview — nothing answers "are my habits working?" (multi-year trends),
"am I keeping it up?" (30-day motivation), or "how recovered am I today?"
(morning report). Multi-year panels over intraday hypertables need pre-aggregation
to render interactively, and robust statistics (percentiles, not min/max) need
`timescaledb_toolkit` — which the companion homelab change is bringing to the
shared cluster. The project is days old: adopting the analytics layer now costs
almost nothing; retrofitting it later means reworking dashboards and caggs.

## What Changes

- **Migration 004 — analytics layer**: hourly continuous aggregates over the
  intraday hypertables, real-time enabled, with refresh policies and `health_ro`
  grants:
  - `heart_rate_hourly`: `percentile_agg` sketch + sample count, per device/source
    (composable to true daily/weekly percentiles in any timezone via `rollup()`)
  - `steps_hourly`, `calories_hourly`, `azm_hourly`: sums per device/source
    (AZM additionally per zone)
- **Views**: `daily_baseline` (per metric per day: value + trailing-30d
  median/p25/p75 — the single shared baseline definition), and
  `sleep_composition` (per-night stage minutes unpacked from
  `sleep_session.levels_summary`).
- **Image pins**: standalone `db` and `migrate` services move from
  `timescale/timescaledb` (Alpine, no toolkit) to the `timescale/timescaledb-ha`
  community tag matching the homelab platform pin. **BREAKING** for existing
  standalone deployments: the data volume cannot be reused across the
  musl→glibc boundary (fresh init + re-run migrations/backfill, or dump/restore).
- **Three provisioned dashboards**:
  - **Trends** (multi-year): percentile-band heart rate, resting HR, weight,
    sleep, HRV, activity volume; device-era background annotation regions;
    a correlation row (activity↔weight, activity↔sleep, activity↔RHR scatters).
  - **Scoreboard** (10–30 days): calendar heatmap, streaks, week-over-week
    deltas vs 30d baseline, WHO activity-minutes target, personal bests.
  - **Morning report**: last night's hypnogram merged with the nighttime HR
    curve as one colored overlay (auto-zoomed to the sleep window), a
    readiness composite in place of the live Sleep Score (unavailable from
    the API sync source — see Amendment below), recovery metrics (HRV, RHR,
    breathing rate, SpO2, skin temp) vs 30d baseline, a nap indicator,
    today's weigh-in, today's live activity so far, yesterday recap, "data
    as of" freshness stat.
- **Docs**: README/dashboard docs updated for the new user journey step; the
  toolkit requirement documented for both deployment modes.

## Capabilities

### New Capabilities
- `health-analytics`: the SQL analytics layer — toolkit availability, hourly
  continuous aggregates (grain, percentile sketches, device/source dimensions,
  real-time reads, refresh policies), baseline and sleep-composition views,
  read-only grants.

### Modified Capabilities
- `health-dashboards`: adds requirements for the three new dashboards
  (trends with device-era regions and correlation row, scoreboard, morning
  report), percentile-based HR rendering, and the shared 30d-median baseline
  semantics.

## Impact

- **Migrations**: new `infra/migrations/004_analytics.sql` (idempotent, owned by
  `health_owner`; requires `timescaledb_toolkit` extension to exist).
- **Compose**: `infra/docker-compose.yml` image pins for `db` and `migrate`.
- **Grafana**: three new dashboard JSONs under `infra/grafana/dashboards/`.
- **Dependency**: ~~shared-mode deployments need the homelab
  `warehouse-db` image swap (toolkit extension) to land first~~ **CLEARED
  2026-07-13**: homelab `warehouse-toolkit-upgrade` landed — `warehouse-db` now
  runs `timescale/timescaledb-ha:pg17.10-ts2.28.2` with `timescaledb_toolkit`
  1.23.0 installed in `warehouse` as a platform guarantee (drill-proven; both
  tenants verified writing). Standalone mode is self-contained after the pin
  change.
- **Docs**: README, `.env.example` untouched (no new config); dashboard docs
  gain the two new journey entries.

## Amendment (2026-07-13)

Exploring a live discrepancy between the stored Sleep Score and the Fitbit
app's score for the same night surfaced that the morning report's "last
night" lookup was broken, not stale: `sleep_session.main_sleep` is a
Takeout-only field —
the Google Health API sync payload has no equivalent, so every query
filtering `WHERE main_sleep` has silently excluded every sync-sourced night
since backfill ended, permanently pinned to the last Takeout-backfilled
night. The same gap freezes `daily_metric`'s `sleep_minutes`/`sleep_score`
rows. Tasks 5.1/5.2 were marked done based on the dashboard rendering, not
on verifying which night it actually rendered — corrected in tasks.md.

Fix and scope addition (see design.md D10–D14 and the updated specs):
a `primary_sleep_session` view replacing the `main_sleep` dependency
everywhere; a locally-computed readiness composite replacing the live Sleep
Score panel (the score itself has no live source — not fixable by better
SQL); the hypnogram merged with the nighttime HR curve as one auto-zoomed
overlay; and morning-report add-ons identified while investigating (overnight
SpO2, a nap indicator, today's weigh-in, today's live activity).
