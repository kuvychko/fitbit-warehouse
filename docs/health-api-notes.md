# Google Health API — spike findings (2026-07-12)

Result of the task-3.1 OAuth/read spike. Everything below was verified live
unless marked *open*. Sample values are synthesized.

## Auth (verified)

- Google Cloud project + **Google Health API** enabled; OAuth consent in
  **Testing** mode (External), self added as test user. All Health scopes are
  **Restricted**, but testing mode needs no verification review.
- **Desktop app** OAuth client works with a loopback redirect
  (`http://localhost:8765/`) — the docs suggest a web client + google.com
  redirect, but desktop/loopback is smoother for a CLI and was accepted.
- Endpoints: auth `https://accounts.google.com/o/oauth2/v2/auth`
  (`access_type=offline&prompt=consent` to guarantee a refresh token), token
  `https://oauth2.googleapis.com/token`. Access tokens live 1 h; refresh
  exchange verified working non-interactively.
- Scopes requested (all three granted in one consent):
  - `googlehealth.activity_and_fitness.readonly`
  - `googlehealth.sleep.readonly`
  - `googlehealth.health_metrics_and_measurements.readonly`
- One-time flow + token storage: `python -m sync.authorize`
  (token at `GOOGLE_TOKEN_PATH`, default `./secrets/google_token.json`).

## API surface (verified against the v4 discovery doc + live calls)

- Base: `https://health.googleapis.com/v4`; discovery:
  `https://health.googleapis.com/$discovery/rest?version=v4` (the marketing
  docs' endpoint table is approximate — trust discovery).
- **Intraday list**: `GET /users/me/dataTypes/{type}/dataPoints` with an
  AIP-160 `filter`; member name depends on the data-type *category*:
  - interval types (steps, distance, …): `steps.interval.start_time`
  - sample types (heart_rate, weight, …): `heart_rate.sample_time.physical_time`
  - daily summaries: `daily_heart_rate_variability.date`
  - sessions: `exercise.interval.civil_start_time`; sleep filters on
    `sleep.interval.end_time` (start-time filtering unsupported for sleep)
  - RFC-3339 literals, `>=` / `<`, `AND`; results ordered by start desc;
    `pageSize` ≤ 10000, `nextPageToken` paging.
- **Daily rollup**: `POST …/dataPoints:dailyRollUp` with body
  `{"range": {"start": {"date": {y,m,d}}, "end": {"date": {y,m,d}}}}`
  (closed-open civil range, `windowSizeDays` default 1).
- **Reconcile**: `GET …/dataPoints:reconcile` (revision sync; candidate for
  catch-up instead of re-listing — investigate during poller build).
- Sample response shape (heart rate; values synthesized):

```json
{"dataPoints": [{
  "dataSource": {"recordingMethod": "PASSIVELY_MEASURED",
                 "device": {"displayName": "Charge 6"}, "platform": "FITBIT"},
  "heartRate": {"sampleTime": {"physicalTime": "2026-01-01T08:09:59Z",
                               "utcOffset": "-25200s",
                               "civilTime": {"date": {...}, "time": {...}}},
                "beatsPerMinute": "72"}}]}
```

  Maps cleanly onto the warehouse schema: `physicalTime` is UTC (matches our
  UTC-everywhere rule), `device.displayName` matches the Takeout `device`
  values ("Charge 6"), numbers arrive as strings.
- Data types mirror the Takeout stream names: `heart-rate`, `steps`,
  `distance`, `total-calories`, `floors`, `oxygen-saturation`,
  `heart-rate-variability`, `daily-heart-rate-variability`,
  `daily-resting-heart-rate`, `sleep`, `weight`, `body-fat`,
  `active-zone-minutes`, `core-body-temperature`, …
- Rate limits: 300 req/min/user, huge per-project quota; 429 + `Retry-After`
  on excess. A daily poller is nowhere near any of this.

## Open items / risks

- **Testing-mode refresh tokens expire after 7 days** (docs). Ours was minted
  2026-07-12 — if `--probe-only` fails after ~2026-07-19, that's why. Planned
  escape hatch: flip the app to **In production without verification** — the
  rate-limit docs define quota for "unverified apps", implying they work
  (one-time scary interstitial). Verify before building the poller's
  scheduling; fallback is periodic Takeout re-exports.
- Restricted-scope verification is only a concern if the app ever needs >100
  users or Google tightens unverified-app policy — irrelevant for personal use
  unless the escape hatch fails.
- `:reconcile` semantics (revisions/tombstones?) unexplored; could make the
  catch-up window cheaper and catch late edits.
