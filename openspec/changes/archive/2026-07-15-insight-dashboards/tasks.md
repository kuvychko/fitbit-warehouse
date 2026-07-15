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
- [x] 2.9 **(added 2026-07-13)** Add `health.primary_sleep_session` view
      (design.md D10): main sleep per civil day = greatest `minutes_asleep`
      (fallback `duration_ms`), computed uniformly regardless of `source`.
      Fix `daily_metric.sleep_minutes` to source from it instead of
      `ss.main_sleep`. Amended in place in `004_analytics.sql` (confirmed
      via the compose file's own migration model: "every migration is
      idempotent... applies all of them on every invocation — there is no
      version table to get out of sync," so this needed no new migration
      number. Validated live against real data: correctly resolves a
      previously-invisible API-synced night that `main_sleep` filtering had
      been skipping.

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

- [x] 5.1 ~~Author `health-morning.json`: hypnogram (state timeline over
      `sleep_stage`), sleep score/efficiency stats, recovery tiles (HRV, RHR,
      breathing rate, temp deviation vs `daily_baseline` with direction
      arrows), yesterday's HR curve + AZM + steps recap~~ **REOPENED
      2026-07-13**: the panels rendered, but "done" was never checked against
      which night they actually showed. `WHERE main_sleep` silently excludes
      every API-synced night (Takeout-only field — see proposal.md
      Amendment, design.md D10), so this had been showing the last
      Takeout-backfilled night indefinitely, not last night. **Re-closed
      2026-07-13** via 5.4–5.8 (rework complete and validated against real
      data to the extent 5.9 describes).
- [x] 5.2 ~~Add "data as of" freshness stat (newest synced sample) and verify
      the pre-sync morning state reads as "still syncing", not
      stale-as-fresh~~ **Freshness stat itself (panel id 1) is fine and
      stays as-is** — it independently reads `max(end_time)` etc. across the
      raw tables, not gated on `main_sleep`. Was never actually broken; the
      reopening was only to avoid re-affirming "done" without re-checking it
      alongside 5.1. Re-confirmed: still correct, unchanged.
- [x] 5.4 **(added 2026-07-13)** Rework "last night" resolution: hypnogram,
      duration/efficiency queries join through `health.primary_sleep_session`
      (2.9) instead of `sleep_session.main_sleep`. Recovery tiles
      (HRV/RHR/breathing/temp) turned out not to need this — they read
      `daily_baseline` directly, never joined `sleep_session` at all.
      Validated live: correctly resolves the most recent API-synced night
      (previously invisible under the old `main_sleep` filter), correctly
      excludes both a same-night nap and a duplicate lower-minutes backfill
      row.
- [x] 5.5 Merge hypnogram into the nighttime HR panel (design.md D12): one
      Grafana annotation-region query per sleep stage (fixed color each),
      layered behind the HR line in a single panel, replacing the separate
      state-timeline panel. Validated live: dashboard reloaded via the 30s
      file-provisioning watcher with all 4 annotation queries and the HR
      panel intact, no provisioning errors; each stage query independently
      confirmed against real data.
- [x] 5.6 **(resolved during implementation, design.md D13)** True ±10min
      auto-zoom isn't achievable: spiked variable-driven `time.from`/
      `time.to` live against Grafana 11.4 — confirmed dead end (stored as
      an inert literal, dateMath doesn't interpolate variables). The
      documented numeric-x-axis fallback conflicts with 5.5's native
      annotation regions (xychart panels can't host them). Resolved with
      the author: panel-level time override (`timeFrom: "13h"`,
      `timeShift: "1h"` → now-14h to now-1h), scoped to this panel only so
      other panels' default range is untouched
- [x] 5.7 Replace the Sleep Score stat with the readiness composite
      (design.md D11, corrected during implementation): vibe score (mean of
      sign-corrected, IQR-scaled `daily_baseline` deviations across
      HRV/RHR/sleep_minutes only — breathing rate/temp are Takeout-only,
      SpO2's baseline was reverted for a perf regression, see D14) and a
      worst-single-metric caution indicator; leave `health.sleep_score` and
      its Trends/Scoreboard usage untouched. Validated live: query returns a
      sensible vibe/caution pair against real baseline data (values omitted
      here — derived from real personal metrics, not fit for a public repo).
- [x] 5.8 Add overnight SpO2 (avg/min over the primary sleep window, raw
      query against `health.spo2`), a nap indicator (any non-primary
      `sleep_session` row for the same day, from 2.9), today's weigh-in
      (raw query against `health.weight`), and a live "today's activity"
      stat (steps/AZM/calories accumulated since local midnight, from the
      existing real-time hourly aggregates). Duration/efficiency panel also
      picked up an honest "n/a (Takeout-only)" label for efficiency instead
      of showing blank or a fabricated estimate — tried a
      minutes_asleep/time_in_bed approximation and rejected it: consistently
      5-6 points below Fitbit's real figure on backfilled nights, which
      would mislead more than an explicit "not available" would.
- [x] 5.9 Re-verify against real data — **verified in production**
      (2026-07-15), not just the dev DB: migration applied cleanly against
      `warehouse-db` (all four files, idempotent no-ops on unchanged
      objects, no errors); `primary_sleep_session` resolves the actual most
      recent night correctly; `daily_baseline.sleep_minutes` now includes
      it; `health_ro` grant confirmed on the new view; the duration/
      efficiency and readiness-composite panel queries both verified
      through Grafana's own query engine (status 200, real data — nulls
      read as nulls, not zeros/errors) against the live Pi Grafana instance.
      The dev-DB blockers noted earlier (stale datasource credential,
      pre-toolkit image) were dev-environment-only and don't apply to the
      production verification path used here.
- [ ] 5.10 End-to-end check after a real 2-hourly poll cycle: report fresh by
      ~09:00 local. **No longer blocked on image cutover** — production
      `warehouse-db` is already on the pinned toolkit image (homelab's
      `warehouse-toolkit-upgrade`) and the Pi's `sync` poller is confirmed
      healthy post-deploy (completed a real write cycle during this
      change's own rollout). What's left is purely observational: watch the
      Morning Report at an actual local morning and confirm it reads fresh
      — not something to force synthetically.

## 6. Docs & wrap-up

- [x] 6.1 Update README + dashboard docs: three new dashboards in the user
      journey, toolkit requirement per deployment mode, Grafana ≥10 note for
      XY charts, backup note (restores need toolkit present)
- [x] 6.2 Screenshots/examples from synthesized data only; confirm no real
      health data in committed JSON or docs (no screenshots shipped, matching
      the existing health-overview.json precedent; all new dashboard JSON
      files are pure SQL templates — grepped for literal real values, none
      found; the throwaway validation container/volume/dump used to test
      queries against real data has been destroyed). **2026-07-15**: this
      round's design.md/tasks.md writeup (done while validating against
      real production data) initially leaked two real values while
      documenting the investigation — a specific Sleep Score comparison and
      a computed readiness score — caught before push and genericized;
      full-diff re-scan clean afterward.
- [x] 6.3 Run gitleaks / repo hygiene pass; update `openspec` status to
      complete — gitleaks ran for real this time as this change's own
      pre-commit hook (`Detect hardcoded secrets ... Passed`), not just a
      manual grep. 5.10 (the only remaining item) is observational, not a
      code blocker — see its note.
