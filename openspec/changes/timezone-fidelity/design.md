# Design: timezone-fidelity

## Context

Follow-up to `health-tenant-foundation` (live: backfill done, poller on the
Pi every 2 h). Policy review found two gaps: the poller discards the API's
per-sample `utcOffset`, and intraday day-bucketing is fixed to a dashboard
timezone while Fitbit-computed dailies are travel-aware — so trip weeks
disagree across panels and with the app. Verified API facts this design
relies on: list-based samples/sessions carry `utcOffset` (e.g. `"-25200s"`);
rollup responses carry none; `dailyRollUp` responses are keyed by
`civilStartTime` (Fitbit's own travel-aware day) and are deduplicated across
devices by Fitbit.

## Goals / Non-Goals

**Goals:**
- Never discard the offset again; make travel visible as data
  (`utc_offset_s` changes = trips).
- `steps_daily` that matches the Fitbit app through any itinerary.
- Purely additive: no rewrites of existing rows, no breaking panel changes.

**Non-Goals:**
- No per-period `TAKEOUT_TZ` overrides for historical backfill (frozen
  history; documented limitation stands).
- No offset for rollup-fed intraday tables (steps/calories/distance/floors)
  — the API does not provide it there.
- No re-attribution of existing intraday rows to local days (NULL offset =
  "unknown, assume home zone").
- Calories/distance/floors dailies: out of scope until a need appears
  (additive later, same pattern).

## Decisions

1. **`utc_offset_s smallint`… no — `integer`** (offsets reach ±50400 s;
   smallint caps at 32767) — nullable, no default, on: `heart_rate`, `spo2`,
   `hrv`, `azm`, `weight`, `body_fat`, `sleep_session`, `sleep_stage`.
   Nullable no-default ADD COLUMN is metadata-only, safe on compressed
   hypertables. Poller mappers parse `utcOffset`/`startUtcOffset` (sleep
   stages use their own segment offsets).
2. **`steps_daily(day date PK, steps integer, source, device NULL)`**,
   hypertable (1-year chunks), action `update` (rollups revise during the
   day). Fed by a new poller puller: `POST …/steps/dataPoints:dailyRollUp`,
   day taken from the response's `civilStartTime.date` (NOT computed from
   physical time), `dataSourceFamily` left default (`all-sources`) because
   dailyRollUp is Fitbit-deduplicated — verify against the app during
   implementation; fall back to `google-wearables` if totals disagree.
3. **History bootstrap via deep pull, not Takeout**: the export has no
   Fitbit daily step totals, and re-deriving them from intraday CSVs would
   re-create the multi-device double-count. One-time
   `python -m sync.poller --bootstrap-steps-daily` (or equivalent flag)
   walks dailyRollUp from 2016-10-01 to now in range-cap-sized chunks.
   Basis era (2015): the backfill's Google Fit parser emits per-day sums of
   Basis intraday steps (single device, `TAKEOUT_TZ` days) into the same
   table with `source='googlefit-takeout'`.
4. **Dashboard**: steps-per-day panel reads
   `SELECT day, max-per-source FROM steps_daily` (max-per-source guards the
   googlefit/api boundary exactly like other panels); the intraday-sum
   query remains available as a panel users can add back for minute-level
   drilldowns.
5. **Ordering**: migration 003 first; poller changes tolerate the column
   already existing (they simply start writing it). Deploy sequence: migrate
   NAS → push/pull Pi → rebuild sync container → run bootstrap once from
   either machine.

## Risks / Trade-offs

- **dailyRollUp deep-pull range cap is unverified for steps** (14-day cap
  documented only for HR/calories/active-minutes). Mitigation: chunked
  requests sized to whatever the first 400 error reports; worst case ~260
  requests for a decade at 14-day chunks — trivial against quota.
- **`all-sources` dailyRollUp totals might not equal the app** (dedup
  semantics unverified). Mitigation: implementation task compares a known
  travel week + a normal week against the app before the panel switches.
- **Mixed provenance in `steps_daily`** (googlefit-takeout 2015, api 2016→):
  same source/device conventions and max-per-source panel guard as
  everywhere else.
