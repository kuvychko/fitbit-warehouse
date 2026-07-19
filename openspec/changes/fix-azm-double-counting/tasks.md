## 1. Confirm the API's AZM field shape (D2 spike)

- [x] 1.1 Query the live Google Health API `active-zone-minutes` data type
      for a recent day with known CARDIO/PEAK minutes; capture the raw
      response shape (does `activeZoneMinutes` expose a raw/unweighted
      value or multiplier alongside the weighted total, or only the
      weighted total?). **Finding: only the weighted total** — FAT_BURN
      1-min interval = value `1`, CARDIO 1-min interval = value `2`, no
      multiplier field present (18 FAT_BURN + 83 CARDIO points sampled,
      ratio exactly 1.0/2.0 with zero exceptions).
- [x] 1.2 Check `:reconcile` semantics for this data type while spiking
      (already flagged unexplored in `docs/health-api-notes.md`) — note
      whether it's a viable path for the historical correction in section 3.
      **Finding: same pre-weighted value convention as `:dataPoints` list**
      — viable as an alternative re-pull source, doesn't avoid the need to
      un-weight in the mapper.
- [x] 1.3 Append findings to `docs/health-api-notes.md` (synthesized sample
      values only, per repo convention — never real payload data with
      identifying details).

## 2. Fix ingestion going forward

- [x] 2.1 Update `map_azm()` in `sync/poller.py` to normalize CARDIO/PEAK
      values to raw per-clock-minute minutes before yielding, per the
      transform chosen from the section 1 spike.
- [x] 2.2 Add a unit test for the fixed mapper (mirroring
      `test_azm_csv_naive_local_to_utc` in `tests/test_parsers.py`),
      covering FAT_BURN and CARDIO/PEAK cases with synthesized values.
      Added `test_map_azm_unweights_cardio_peak` to
      `tests/test_poller_mappers.py`.
- [x] 2.3 Run the full test suite; confirm no regression in other mappers.
      26/26 passed (local `.venv` was stale/broken — pointed at a removed
      Python 3.12 install — recreated against the available Python 3.14
      and reinstalled `requirements.txt` + `pytest` to run this).
- [x] 2.4 Deploy the fix (poller container restart on the Pi). Committed
      (3616686), pushed to `origin/main`, pulled on the Pi
      (`iaq-server`), `docker compose --profile sync up -d --build sync`.
- [x] 2.5 Confirm at least one post-deploy sync cycle writes correctly
      normalized values for a fresh CARDIO/PEAK interval. First post-deploy
      cycle ran but wrote 0 new azm rows — discovered `health.azm`'s
      upsert is `ON CONFLICT DO NOTHING` (immutable-sample table), so
      already-existing (time, zone) keys from before the fix silently
      blocked the corrected values from ever landing. This directly fed
      into section 3's approach (delete-then-repull, not a bare re-pull).

## 3. Correct historical data (D3)

- [x] 3.1 Determine the exact affected window: earliest `source = 'api'`
      row in `health.azm` with `zone IN ('CARDIO', 'PEAK')` through the
      deploy time in task 2.4. **Found: 353 rows, 2026-07-09 17:46 through
      2026-07-19 16:35 UTC.** Confirmed every single one carried the
      pre-weighted value verbatim (CARDIO/PEAK minutes was always exactly
      `2`, FAT_BURN always exactly `1` — no exceptions).
- [x] 3.2 Attempt re-pull of `active-zone-minutes` for the affected window
      through the fixed mapper, scoped to just that data type/cursor (not a
      full `run_cycle`) — preferred correction path per D3. Given the
      DO-NOTHING finding in 2.5, this required deleting the 353 stale rows
      first (via trusted local `psql` on the NAS as `health_owner` —
      `health_rw`, the poller's role, has no DELETE grant by design), then
      re-running `pull_list("active-zone-minutes", ...)` through the fixed
      `map_azm` for 2026-07-09 through now. Result: 353 rows re-written.
      Post-correction: CARDIO sum 466→233 (exactly halved), PEAK sum
      240→120 (exactly halved), FAT_BURN untouched at 68 — matching the
      un-weighting transform exactly.
- [x] 3.3 (Not needed — re-pull in 3.2 succeeded.)
- [x] 3.4 (Not needed — re-pull in 3.2 succeeded; no rows left uncorrected.)
- [x] 3.5 Manually refresh `health.azm_hourly` over the corrected (or
      decided-to-leave) date range so the continuous aggregate reflects the
      outcome without waiting for the next scheduled refresh. Ran
      `CALL refresh_continuous_aggregate('health.azm_hourly', '2026-07-09
      00:00:00+00', now())` on the NAS.

## 4. Verify

- [x] 4.1 Compare the Morning Report's "AZM" and "AZM today" tiles against
      the Fitbit app for at least one full day post-fix. Ran the exact
      dashboard SQL against corrected data for 2026-07-19: **174**, vs. the
      Fitbit app's **173** reported earlier in this session (the 1-minute
      gap is just additional data synced since that reading) — matches.
- [x] 4.2 Compare the Scoreboard's WHO 150 min/week AZM bar against
      expectations for a week spanning the corrected range. Ran the exact
      dashboard SQL (trailing 7d): **596**, a plausible weighted total, no
      sign of residual doubling.
- [x] 4.3 Visually confirm both panels render as expected in Grafana (per
      this repo's Grafana-panel-verification lesson — a JSON/SQL review
      alone isn't sufficient confirmation). Confirmed by the user: Grafana
      looks good, numbers make sense.
