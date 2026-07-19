## Why

Active Zone Minutes (AZM) synced via the Google Health API are inflated ~2x
for CARDIO/PEAK zone minutes. Confirmed on 2026-07-19: Fitbit's own app
showed 173 AZM for the day; the Morning Report dashboard showed 338. The
Google Health API's `activeZoneMinutes.activeZoneMinutes` field returns the
already-weighted score (CARDIO/PEAK minutes pre-multiplied by 2), but
`sync/poller.py`'s `map_azm()` stores it into `health.azm.minutes` as if it
were raw, unweighted per-minute zone occupancy — the same convention used
for the Takeout CSV backfill path (confirmed genuinely raw via
`tests/test_parsers.py`). The Grafana dashboard layer then applies the
CARDIO/PEAK `* 2` weighting on top (`health-morning.json`), which is correct
for backfill data but compounds the API's already-weighted values to 4x.

Every `source = 'api'` row in `health.azm` for CARDIO/PEAK zones since the
sync poller went live carries this inflation, so it isn't limited to the
Morning Report tile — the Scoreboard's WHO 150 min/week bar and any
trend/range panel over synced days are overstated too.

## What Changes

- Normalize AZM ingestion so `health.azm.minutes` is always raw,
  per-clock-minute, unweighted zone occupancy regardless of `source` —
  matching the invariant the backfill path already satisfies and the one
  the dashboard query layer already assumes when it applies the CARDIO/PEAK
  `* 2` weighting. Concretely: fix `map_azm()` in `sync/poller.py` to
  un-weight the API's CARDIO/PEAK values before storing them (exact
  transform TBD in design — depends on confirming the API's actual field
  semantics).
- Evaluate correcting historical `source = 'api'` rows already inflated in
  `health.azm` (and, transitively, `health.azm_hourly`). Options to weigh in
  design: re-pull from the API (bounded by how far back it can be queried
  vs. the poller's `CATCHUP_CAP_DAYS` catch-up cap), a one-off corrective
  `UPDATE` halving affected rows, or leaving history as-is with the
  limitation documented. Fixing ingestion going forward is required; fixing
  history is desired but may be dropped if too risky/imprecise — that
  tradeoff is decided in design.md, not assumed here.
- No change to the dashboard query layer itself (`* 2` for CARDIO/PEAK stays
  — it's correct once the underlying data is raw) unless design finds a
  reason otherwise.

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `health-api-sync`: AZM ingestion must normalize values from the Google
  Health API to raw, unweighted per-zone minutes before storage, so
  `health.azm.minutes` carries the same semantics regardless of `source`.

## Impact

- **Code**: `sync/poller.py` (`map_azm`), plus test coverage for the
  un-weighting transform (`tests/` currently has no API-side AZM mapper
  test, only the CSV parser one).
- **Data**: `health.azm` (`source = 'api'`, `zone IN ('CARDIO','PEAK')`
  rows) — possible corrective update or re-pull, pending design decision.
  Downstream: `health.azm_hourly` continuous aggregate inherits whatever
  correction (or non-correction) is applied to the raw table; may need a
  manual refresh over the corrected range.
- **Dashboards**: `infra/grafana/dashboards/health-morning.json` and
  `health-scoreboard.json` read corrected data automatically once
  `health.azm`/`health.azm_hourly` are fixed — no panel JSON changes
  expected, but should be visually re-verified per this repo's Grafana
  panel-verification lesson (CLAUDE.md).
- **No API/schema contract changes**: `health.azm`'s column shapes and the
  Google Health API request/response handling elsewhere are untouched.
