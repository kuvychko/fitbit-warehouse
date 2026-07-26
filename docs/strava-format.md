# Strava bulk export format → warehouse mapping

What a Strava bulk account export actually contains and how it maps to the
`health` schema. Based on a July 2026 export; Strava's export format is
undocumented and can drift — the loader reports anything it doesn't recognize.

All sample values below are **synthesized**, not real data (no real GPS
coordinates, activity names, or timestamps — same rule as the rest of this
repo).

## Requesting the export

Strava → Settings → *My Account* → "Download or Delete Your Account" →
*Get Started* → **Request your archive**. Strava emails a `.zip` (often within
minutes to a few hours). Extract it; point `STRAVA_EXPORT_DIR` (or
`--strava-dir`) at the extracted directory (the one containing
`activities.csv`).

## What's in the zip

| Path | Loaded? | Notes |
|---|---|---|
| `activities.csv` | **yes** | One row per activity — the summary source of truth |
| `activities/<id>.gpx` | **yes** | One GPS track per activity (filename = activity ID) |
| `activities/<id>.fit`/`.tcx` | no | Original recordings — GPX is the fidelity ceiling here |
| `clubs.csv`, `followers.csv`, `gear.csv`, `comments.csv`, media, … | no | Social-graph / account data, explicitly out of scope |

Everything except `activities.csv` + `activities/*.gpx` is recognized and
skipped (reported in the run summary), never flagged as unknown.

## `activities.csv` — the traps that matter

The file is wide (~100 columns) and has two quirks the loader deliberately
handles:

1. **Duplicate column names.** Several headers repeat. In particular there are
   **two** `Distance` columns and **two** `Elapsed Time` columns:
   - The *first* `Distance` is in the **account's display unit** (km *or*
     miles, depending on the athlete's setting) — **not portable**.
   - The *second* `Distance` is always in **meters**. The loader reads the
     **last** `Distance` occurrence for `distance_m`.
   - Both `Elapsed Time` columns carry the same seconds value; the loader uses
     the detailed (second) one for `elapsed_time_s`.
   Because names repeat, the parser resolves columns **by name + occurrence**,
   not by fixed position.

2. **`Activity Date` is UTC.** e.g. `Jul 15, 2026, 6:08:13 PM` parses as
   `2026-07-15T18:08:13Z` — verified against each GPX's `<metadata><time>`,
   which matches exactly. Stored as a UTC `timestamptz`, like every other time
   column in the schema.

3. **No `sport_type`.** The export carries only the coarse `Activity Type`
   (`Run`, `Hike`, `Ride`, …); the granular `sport_type` (`TrailRun`,
   `VirtualRun`, …) that the *API* exposes is **not** in the bulk export (there
   is a `Type` column, but it is empty). So both backfill and sync filter on
   the coarse type — `STRAVA_ACTIVITY_TYPES` (default `Run,Hike`) — to stay in
   parity.

### Columns the loader reads → `health.strava_activity`

| CSV column (occurrence) | → column | Notes |
|---|---|---|
| `Activity ID` | `activity_id` | bigint, immutable natural key |
| `Activity Date` | `start_time` | UTC |
| `Activity Name` | `name` | |
| `Activity Type` | `activity_type` | coarse type; drives the allowlist |
| `Distance` (last) | `distance_m` | meters |
| `Moving Time` | `moving_time_s` | seconds |
| `Elapsed Time` (last) | `elapsed_time_s` | seconds |
| `Elevation Gain` | `elev_gain_m` | meters |
| `Elevation Loss` | `elev_loss_m` | meters |
| — | `utc_offset_s` | `NULL` for backfill (the CSV has no offset; the API supplies it) |
| — | `source` | `strava-export` |

Summary values go in **verbatim** — no recomputing distance/elevation/pace from
the GPX track.

## `activities/<id>.gpx` — the tracks → `health.activity_track`

Plain GPX 1.1, one `<trkpt lat lon>` per fix at ~1 Hz, each with a child `<ele>`
(meters) and `<time>` (absolute UTC). Synthesized shape:

```xml
<gpx xmlns="http://www.topografix.com/GPX/1/1">
  <metadata><time>2026-07-15T18:08:13Z</time></metadata>
  <trk><trkseg>
    <trkpt lat="37.000000" lon="-122.000000">
      <ele>10.0</ele><time>2026-07-15T18:08:13Z</time>
    </trkpt>
    <!-- … one per second … -->
  </trkseg></trk>
</gpx>
```

- **No `<extensions>`** — the bulk GPX carries **no** heart rate, cadence, or
  power. (HR correlation is a query-time join against `health.heart_rate`; see
  `health.strava_activity_hr`.)
- Each `<trkpt>` becomes one `activity_track` row (`lat`, `lon`, `elevation_m`
  unmodified). A generated `geography(Point,4326)` column + GiST index are
  populated automatically for geo queries.
- **`time` is derived, not copied:** `start_time + round(trkpt_time -
  start_time)`. GPX times are already whole-second `start + offset`, so this
  matches the API sync's `start_date + offset` exactly — a GPX-loaded track and
  an API-synced track for the same activity share `(activity_id, time)` keys and
  dedup instead of duplicating.

A 68-minute run is ~4,000 points; a full history of a few hundred runs is on the
order of 1M rows — the same shape as the heart-rate hypertable, with the same
TimescaleDB compression applied.

## Running the backfill

```
python -m backfill --strava-dir ./data/strava           # or STRAVA_EXPORT_DIR
```

Idempotent: natural-key upserts absorb re-runs and any overlap with API-synced
data. The run summary reports activities skipped by type, allowlisted
activities missing a GPX (summary still loads), and orphan GPX files with no
CSV row.
