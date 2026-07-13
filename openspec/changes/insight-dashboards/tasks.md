## 1. Platform prerequisite & image pins

- [x] 1.1 Confirm the homelab `warehouse-db` swap has landed (timescaledb-ha
      community tag, `timescaledb_toolkit` created in the `warehouse` DB) —
      shared-mode blocker for everything below
- [x] 1.2 Pin `db` and `migrate` in `infra/docker-compose.yml` to the same
      `timescale/timescaledb-ha` community tag as the platform (target
      `pg17.10-ts2.28.2`); verify standalone profile boots on a fresh volume
      and migrations 001–003 pass on it
- [x] 1.3 Document the BREAKING standalone volume note (musl→glibc: fresh
      volume + re-run migrations/backfill) in README

## 2. Migration 004 — analytics layer

- [x] 2.1 Write `infra/migrations/004_analytics.sql` preamble: idempotent
      guard that `timescaledb_toolkit` exists (create if privilege allows,
      else fail with a clear message), `SET ROLE health_owner`
- [x] 2.2 Create `heart_rate_hourly` cagg: `time_bucket('1 hour')`, device,
      source, `percentile_agg(bpm)`, `count(*)` samples; real-time enabled
- [x] 2.3 Create `steps_hourly`, `calories_hourly`, `azm_hourly` caggs (sums
      per device/source; AZM keyed additionally by zone)
- [x] 2.4 Add refresh policies (daily consolidation; start_offset verified
      against the 90-day compression policy semantics of the pinned version)
- [x] 2.5 Create `daily_baseline` view (LATERAL trailing-30d
      median/p25/p75 for RHR, HRV, minutes asleep, sleep score, breathing
      rate, nightly temp deviation, daily steps)
- [x] 2.6 Create `sleep_composition` view unpacking `levels_summary` (stages
      and classic vocabularies)
- [x] 2.7 Grants: `SELECT` on all new objects to `health_ro`; verify
      idempotency by running 004 twice
- [x] 2.8 Validate cagg correctness on real data: daily percentiles from
      rolled-up sketches vs raw `percentile_cont` spot-check days; sums vs
      `steps_daily` dedup behavior

## 3. Trends dashboard

- [x] 3.1 Author `health-trends.json`: HR percentile-band panel (rollup to
      civil days in `$timezone`, p25–p75 band + median line, min-samples
      guard), RHR, weight, sleep duration + composition, HRV, activity volume
      with rolling-median smoothing (read the dataviz skill first)
- [x] 3.2 Add device-era annotation queries (per-device min/max hour from
      `heart_rate_hourly`) rendered as background regions with per-device hues
- [x] 3.3 Add year-over-year overlay and month-of-year seasonality panels
- [x] 3.4 Add correlation row (XY charts: 28d steps → weigh-in; prev-day
      activity → sleep score; 28d activity → RHR), year-colored points,
      lag-direction titles
- [x] 3.5 Verify full-history range renders interactively (no raw intraday
      scans in any panel query)

## 4. Scoreboard dashboard

- [x] 4.1 Author `health-scoreboard.json`: calendar heatmap of `steps_daily`,
      streak counters (gaps-and-islands over civil days in `$timezone`),
      week-over-week delta tiles vs `daily_baseline`, personal bests, WHO
      150 min/week AZM panel
- [x] 4.2 Verify baseline-relative framing (tiles show deviation from 30d
      median) and behavior in a low-activity week (informative, not broken)

## 5. Morning report dashboard

- [x] 5.1 Author `health-morning.json`: hypnogram (state timeline over
      `sleep_stage`), sleep score/efficiency stats, recovery tiles (HRV, RHR,
      breathing rate, temp deviation vs `daily_baseline` with direction
      arrows), yesterday's HR curve + AZM + steps recap
- [x] 5.2 Add "data as of" freshness stat (newest synced sample) and verify
      the pre-sync morning state reads as "still syncing", not stale-as-fresh
- [ ] 5.3 End-to-end check after a real 2-hourly poll cycle: report fresh by
      ~09:00 local (blocked on the live standalone stack actually cutting over
      to the new image — see note below; can't validate against a live poller
      until that happens)

## 6. Docs & wrap-up

- [x] 6.1 Update README + dashboard docs: three new dashboards in the user
      journey, toolkit requirement per deployment mode, Grafana ≥10 note for
      XY charts, backup note (restores need toolkit present)
- [x] 6.2 Screenshots/examples from synthesized data only; confirm no real
      health data in committed JSON or docs (no screenshots shipped, matching
      the existing health-overview.json precedent; all new dashboard JSON
      files are pure SQL templates — grepped for literal real values, none
      found; the throwaway validation container/volume/dump used to test
      queries against real data has been destroyed)
- [ ] 6.3 Run gitleaks / repo hygiene pass; update `openspec` status to
      complete (gitleaks binary unavailable in this environment — ran a
      manual secret-pattern grep across all new/changed files instead, clean;
      re-run the real `pre-commit run --all-files` before pushing. Not
      marking openspec status complete: 5.3 is still genuinely blocked)
