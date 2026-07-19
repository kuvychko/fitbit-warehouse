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
- [ ] 2.4 Deploy the fix (poller container restart on the Pi).
- [ ] 2.5 Confirm at least one post-deploy sync cycle writes correctly
      normalized values for a fresh CARDIO/PEAK interval.

## 3. Correct historical data (D3)

- [ ] 3.1 Determine the exact affected window: earliest `source = 'api'`
      row in `health.azm` with `zone IN ('CARDIO', 'PEAK')` through the
      deploy time in task 2.4.
- [ ] 3.2 Attempt re-pull of `active-zone-minutes` for the affected window
      through the fixed mapper, scoped to just that data type/cursor (not a
      full `run_cycle`) — preferred correction path per D3.
- [ ] 3.3 If re-pull is impractical (API retention/range limits, or
      `:reconcile` findings suggest otherwise), write a scoped one-off
      corrective script (not part of `infra/migrations/`) halving
      `minutes` for `source = 'api' AND zone IN ('CARDIO','PEAK')` rows
      within the confirmed affected time range only.
- [ ] 3.4 If neither 3.2 nor 3.3 is workable, document the decision to
      leave history uncorrected as a known limitation — in
      `docs/health-api-notes.md` and as a note visible near the affected
      dashboard panels — and record the affected date range.
- [ ] 3.5 Manually refresh `health.azm_hourly` over the corrected (or
      decided-to-leave) date range so the continuous aggregate reflects the
      outcome without waiting for the next scheduled refresh.

## 4. Verify

- [ ] 4.1 Compare the Morning Report's "AZM" and "AZM today" tiles against
      the Fitbit app for at least one full day post-fix.
- [ ] 4.2 Compare the Scoreboard's WHO 150 min/week AZM bar against
      expectations for a week spanning the corrected range.
- [ ] 4.3 Visually confirm both panels render as expected in Grafana (per
      this repo's Grafana-panel-verification lesson — a JSON/SQL review
      alone isn't sufficient confirmation).
