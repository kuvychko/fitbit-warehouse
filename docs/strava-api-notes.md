# Strava API notes (sync poller)

Quirks of the Strava API v3 integration (`sync/strava_authorize.py`,
`sync/strava_poller.py`) that aren't obvious from the code. Companion to
`docs/health-api-notes.md` (the Google Health API equivalent).

## OAuth: the refresh token **rotates** on every refresh

This is the single most important difference from the Google flow, and the
easiest thing to break by "simplifying":

- Every call to `POST /oauth/token` with `grant_type=refresh_token` returns a
  **new** `access_token` *and* a **new** `refresh_token`. The old refresh token
  is invalidated **server-side, immediately**.
- Therefore the poller must **persist the new refresh token after every
  refresh**, not just at initial authorization. Google's
  `refresh_access_token()` (which returns only the access token and never writes
  back) is correct there — its refresh token is stable — and **fatal** here.
- The write is **atomic** (temp file + `os.replace`) and happens **before** the
  fresh access token is used for any fallible request. A crash between "refresh
  succeeded" and "token persisted" would otherwise strand the poller with a dead
  refresh token, requiring a manual re-auth — a real hazard on an SD-card /
  power-loss deployment.
- Consequence for ops: the token file (`STRAVA_TOKEN_PATH`) is **mutable state**,
  not a write-once secret. Its Docker mount is **read-write** (`../secrets:/secrets`,
  *not* `:ro` like the Google sync). Don't run two pollers against the same token
  file — each refresh invalidates the other's.

One-time setup:

```
python -m sync.strava_authorize          # opens a browser; scope: activity:read_all
```

The app is a personal "Single Player" application (Strava → Settings → API);
set the *Authorization Callback Domain* to `localhost`.

## Base URL migration (January 2027)

Strava is moving the data API from `https://www.strava.com/api/v3` to
`https://api-v3.strava.com` on **2027-01-04**. The base URL is `.env`-driven
(`STRAVA_API_BASE`) precisely so this is a **config change, not a code change** —
update the variable and restart. The **OAuth** endpoints
(`www.strava.com/oauth/*`) are *not* affected and stay hardcoded.

## Endpoints used

| Endpoint | Use |
|---|---|
| `GET /athlete/activities?after=<epoch>&page=&per_page=200` | List new activities since the catch-up cursor. No server-side type filter — the allowlist is applied client-side on the coarse `type`. |
| `GET /activities/{id}/streams?keys=time,latlng,altitude&key_by_type=true` | GPS track for an activity not yet in `activity_track`. Returns `{time:{data:[…]}, latlng:{data:[[lat,lon],…]}, altitude:{data:[…]}}`. |

- **`type` vs `sport_type`:** the poller filters on the coarse `type` field to
  match the bulk export (which has no `sport_type`; see `docs/strava-format.md`).
  If Strava ever drops `type`, derive the coarse value from `sport_type` via a
  downcast (`TrailRun`→`Run`, …) — the one place that mapping would live.
- **Track existence guard:** streams are fetched only for activities with **no**
  rows in `activity_track`. This is load-bearing, not an optimization — it stops
  a normal sync from re-deriving a backfilled run's track. Each point's time is
  `start_date + offset`, identical to the backfill, so even when both paths do
  touch an activity the keys match and dedup.
- **No stream (404):** manual / treadmill activities with no GPS return 404 on
  `/streams`; the poller skips them (summary still upserts).

## Catch-up cursor

`after = max(start_time) in strava_activity − STRAVA_OVERLAP_HOURS` (default
24 h). The overlap means an activity uploaded shortly before the previous
cursor is never skipped; re-listing already-stored activities is free (they
upsert by ID). When the table is empty (before any backfill) the cursor is `0`
(all history).

## Rate limits

Strava's limits (per application) are comfortably above what one athlete's
activity list needs at a 6 h cadence: a cycle is one list call plus one stream
call per *new* activity. HTTP 429 is treated as expected — the cycle ends
cleanly (`CycleAbort`) and the next cycle's catch-up window covers the gap; no
busy-retry, no crash-loop. A Healthchecks ping fires only after a fully
successful cycle (`STRAVA_HEALTHCHECKS_PING_URL`).
