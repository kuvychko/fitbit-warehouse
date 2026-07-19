## Context

`health.azm` stores one row per `(time, zone)`: a clock-minute and which
heart-rate zone (FAT_BURN/CARDIO/PEAK) it fell in. The table has never
stored a weighted value — by design, the *only* place Fitbit's official
1x/2x-for-cardio-or-above AZM weighting is applied is the Grafana query
layer (`health-morning.json`'s `sum(minutes) FILTER (zone IN
('CARDIO','PEAK')) * 2 + sum(minutes) FILTER (zone = 'FAT_BURN')`). That
holds for the Takeout CSV backfill path: `tests/test_parsers.py` confirms
`total_minutes` is raw (a single FAT_BURN clock-minute stores as `1`).

It does not hold for the API sync path. `sync/poller.py`'s `map_azm()`
copies `activeZoneMinutes.activeZoneMinutes` from the Google Health API
straight into `health.azm.minutes`, on the same raw-value assumption — but
that field's name is literally Google/Fitbit's product name for the
*weighted* score, and live data confirms it already carries the weighting:
on 2026-07-19 the Fitbit app showed 173 AZM for the day; the warehouse
showed 338. Solving `F + 2C = 173` (app, correct) against `F + 4C = 338`
(ours, both layers applying the 2x) gives `C ≈ 82.5`, `F ≈ 8` raw
minutes — a plausible split for a 90-minute run mostly in cardio/peak, and
consistent with CARDIO/PEAK being weighted twice while FAT_BURN (always
1x) is untouched in both numbers.

`docs/health-api-notes.md` (the original API spike doc) never captured a
sample AZM payload the way it did for heart rate — this is a genuine gap in
what was verified live, not a case of overlooking known documentation.

## Goals / Non-Goals

**Goals:**
- Make `health.azm.minutes` carry the same raw, unweighted, per-clock-minute
  semantics regardless of `source`, restoring the invariant the dashboard
  query layer already assumes.
- Decide, with an explicit recommendation, whether/how to correct already-
  inflated `source = 'api'` historical rows — not just fix new data.
- Keep the fix scoped to AZM ingestion; don't touch unrelated mappers or the
  dashboard's weighting logic unless the API investigation forces it.

**Non-Goals:**
- Recomputing heart-rate zones from raw bpm (explicitly rejected in the
  original insight-dashboards design — Fitbit's own zone assignment stays
  the source of truth).
- Changing the dashboard's `* 2` CARDIO/PEAK weighting query — it's correct
  once the underlying data is raw; no reason found yet to move weighting
  out of the query layer.
- Auditing every other API mapper for the same class of bug. AZM is the one
  field whose *name* doubles as the product's term for an already-weighted
  metric; other mapped fields (bpm, kg, meters, etc.) don't have that
  ambiguity. Worth a passing mention as a lesson learned, not a scope
  expansion here.

## Decisions

### D1. Normalize at ingestion, not at query time
Fix `map_azm()` so `health.azm.minutes` is always raw, rather than adding a
`WHERE source = 'api'` branch to the dashboard's weighting math.
*Alternative rejected*: source-conditional weighting in every dashboard
query that reads `azm`/`azm_hourly` (Morning Report today/yesterday tiles,
Scoreboard's WHO 150 min/week bar, any future trend panel) would duplicate
the same conditional in multiple Grafana panel JSON files and silently
break again the next time a panel is added — the table is supposed to be
the one place all sources agree on meaning.

### D2. Confirm the API's exact field shape before writing the transform
The classic Fitbit Web API exposed both a zone's raw `minutes` and its
`minuteMultiplier` side by side; it's unconfirmed whether the Google Health
API's `activeZoneMinutes` object exposes an equivalent explicit multiplier
or only the pre-weighted total. Spike this against a live payload (same
rigor as the original `docs/health-api-notes.md` spike) before finalizing:
- If an explicit multiplier/raw-value field exists, use it directly —
  robust to any future zone taxonomy change.
- If only the weighted total exists, divide by the zone's known weight
  (CARDIO/PEAK ÷ 2, FAT_BURN ÷ 1) via the same hardcoded zone-name mapping
  the dashboard already uses — consistent style, and matches the fact nothing
  elsewhere in this codebase recomputes zone weights dynamically.
Document findings in `docs/health-api-notes.md` (append), per this repo's
existing convention for recording verified API behavior.

### D3. Historical correction: prefer re-pull, fall back to a scoped corrective UPDATE
The affected window is currently narrow — the API sync went live
2026-07-12; this was caught 2026-07-19, so at most ~1 week of `source =
'api'` AZM rows are inflated. Recommendation, in order of preference:
1. **Re-pull the `active-zone-minutes` data type** for the affected window
   through the *fixed* `map_azm`, scoped to just that table/cursor (not a
   full `run_cycle`, so other streams' catch-up cursors aren't disturbed).
   Most correct — goes through the same normalized path as all future data.
   Feasible here because the corrupted window is small and recent, well
   within whatever retention the API turns out to have.
2. **Scoped corrective `UPDATE`** (`source = 'api' AND zone IN ('CARDIO',
   'PEAK')`, halving `minutes`) if re-pull proves impractical (e.g. the API
   won't return that far back, or `:reconcile` semantics complicate a clean
   re-pull). Must be a one-off script, explicitly non-idempotent and
   guarded to a specific time range — never folded into `infra/migrations/`
   (those are re-run on every `migrate` invocation; a repeatable `/2` would
   silently corrupt the same rows again on a later replay).
3. **Leave history as-is**, documented as a known limitation (dashboard
   note + `docs/health-api-notes.md` entry), only if both above are judged
   too risky/impractical. Given how small and recent the window currently
   is, this should rarely be necessary — but the longer this fix is
   delayed, the larger and more entrenched the corrupted window gets, so
   there's mild time pressure to ship before falling back to this option
   becomes the only realistic choice.

*Alternative rejected*: a blanket "divide all historical CARDIO/PEAK API
rows by 2 forever, no investigation" — skips confirming D2's actual field
shape, and blind halving is only correct if the inflation factor really is
always exactly 2x with no rounding/edge cases (e.g. partial-minute
intervals) — worth the one live spike to be sure before mutating data.

### D4. Refresh `azm_hourly` after any historical correction
`azm_hourly` is a real-time continuous aggregate
(`materialized_only = false`, `004_analytics.sql`), so its non-materialized
region reflects raw-table corrections immediately; the materialized region
refreshes on the existing daily policy (`start_offset => 31 days`), which
comfortably covers the ~1-week affected window. Still, manually call
`refresh_continuous_aggregate` over the corrected range right after the
correction rather than waiting for the next scheduled cycle, since the
Morning Report is meant to reflect "as of now."

## Risks / Trade-offs

- [D2's field-shape assumption turns out wrong even after the spike] →
  mitigate by testing the new `map_azm` against a captured real payload
  (unit test, mirroring `test_azm_csv_naive_local_to_utc`) before deploying,
  not just against synthesized guesses.
- [Corrective UPDATE accidentally re-run, double-halving already-fixed
  rows] → keep it a standalone script scoped to an explicit time range and
  `source = 'api'`, run manually once, never added to the idempotent
  migration path.
- [Re-pull disturbs other data types' catch-up cursors] → scope the re-pull
  to the `active-zone-minutes` puller only; don't invoke full `run_cycle`.
- [Fix ships but nobody visually re-checks the panels] → per this repo's
  known Grafana-panel-verification lesson (CLAUDE.md), visually compare
  Morning Report + Scoreboard AZM numbers against the Fitbit app for at
  least one full day post-fix, not just a JSON/SQL review.
- [Weighting logic now lives in two places conceptually — ingestion
  normalizes to raw, query layer re-applies the weight] → this isn't new
  duplication, it's finally matching the invariant the design always
  intended (table = raw, query = weighted); accepted.

## Migration Plan

1. Spike the live AZM API payload shape; document in
   `docs/health-api-notes.md` (D2).
2. Fix `map_azm()` to normalize CARDIO/PEAK values to raw minutes; add a
   unit test for the mapper.
3. Deploy (poller container restart on the Pi) — new pulls are correct from
   this point forward.
4. Execute the chosen historical-correction path (D3) scoped to the
   affected `source = 'api'` window.
5. Manually refresh `health.azm_hourly` over the corrected range (D4).
6. Visually re-verify the Morning Report and Scoreboard AZM panels against
   the Fitbit app.

**Rollback**: no schema change, so reverting is just reverting the
`map_azm` code change. If a historical correction was applied and later
found wrong, the rollback is re-doubling the affected rows — mitigated by
spiking the field shape (D2) before mutating any data.

## Open Questions

- Does the Google Health API's `activeZoneMinutes` object expose an
  explicit multiplier/raw-value field, or only the pre-weighted total?
  (Resolves D2 — spike before implementing.)
- How far back can `active-zone-minutes` actually be queried/re-pulled
  (retention limit)? Determines whether D3's re-pull option can cover the
  whole affected window.
- Would `:reconcile` (flagged unexplored in `docs/health-api-notes.md`)
  give a cleaner authoritative correction than re-listing? Worth a quick
  look during the same spike since it's already an open item there.
