# Google Takeout export format → warehouse mapping

What the Takeout zips actually contain and how each stream maps to a table in
the `health` schema. Based on a July 2026 export; Takeout formats are
undocumented and can drift — the loader validates every file and reports
anything it does not recognize (see `backfill/`).

All sample rows below are **synthesized**, not real data.

## The two exports

| Export | Takeout product | Contents |
|---|---|---|
| Fitbit | "Google Health" | Your full Fitbit history (`Takeout/Google Health/…`) |
| Google Fit *(optional)* | "Fit" | Anything that ever synced to Google Fit — e.g. **Basis Peak** data from 2015 (`Takeout/Fit/…`) |

Request both at <https://takeout.google.com> if you have legacy Google Fit
data; otherwise the Fitbit export alone is the normal path.

## Fitbit export: two parallel formats

Most streams appear **twice**:

1. **`Global Export Data/`** — classic per-day/per-month JSON
   (`heart_rate-2024-05-01.json`, `sleep-2024-05-23.json`, …).
   ⚠️ Timestamp *values* are UTC, but each day file is bounded by *local* day
   (`"05/01/24 07:00:07"` = 07:00 **UTC**). No device information.
2. **`Physical Activity_GoogleData/`** — newer per-day/per-month CSVs
   (`heart_rate_2024-05-01.csv`, …) with ISO-8601 UTC timestamps, a
   `data source` column (device name), and a `*_readme.txt` per stream.

**Rule: CSV wins.** Where a stream exists in both formats the loader reads the
CSV and reports the JSON twin as intentionally skipped. Classic JSON is used
only where no CSV equivalent exists (sleep sessions/stages, daily summaries).

## Stream → table mapping

### Loaded from `Physical Activity_GoogleData/` CSVs (UTC, has device)

| Files | Grain | → table | Notes |
|---|---|---|---|
| `heart_rate_YYYY-MM-DD.csv` | ~5–15 s, daily files | `heart_rate` | `beats per minute` |
| `steps_YYYY-MM-DD.csv` | per-minute, monthly files | `steps` | |
| `calories_YYYY-MM-DD.csv` | per-minute, monthly | `calories` | kcal |
| `distance_YYYY-MM-DD.csv` | per-minute, monthly | `distance` | meters (verify against readme) |
| `floors_YYYY-MM-DD.csv` | per-minute, monthly | `floors` | |
| `oxygen_saturation_YYYY-MM-DD.csv` | ~1–3 min, monthly | `spo2` | preferred over `Minute SpO2 - *.csv` (no device col) and `estimated_oxygen_variation` (different metric) |
| `heart_rate_variability_YYYY-MM-DD.csv` | 5 min (sleep), monthly | `hrv` | rmssd + sdrr; the HRV-folder "Details" variant (naive timestamps, lf/hf) is skipped |
| `respiratory_rate_sleep_summary_YYYY-MM-DD.csv` | per sleep, monthly | `breathing_rate` | header says "milli breaths per minute" but values are plain breaths/min — sanity-checked at parse |
| `body_fat_YYYY-MM-DD.csv` | sparse | `body_fat` | |

### Loaded from other CSV folders

| Files | Grain | → table | Notes |
|---|---|---|---|
| `Active Zone Minutes (AZM)/Active Zone Minutes - *.csv` | per-minute, monthly | `azm` | ⚠️ naive local timestamps (`2024-05-01T09:07`); zone ∈ FAT_BURN/CARDIO/PEAK |
| `Temperature/Device Temperature - *.csv` | per-minute, daily | `device_temperature` | ⚠️ naive local timestamps; value is a *deviation*, not absolute °C |
| `Temperature/Computed Temperature - *.csv` | per sleep, monthly | `nightly_temperature` | ⚠️ naive local timestamps; absolute nightly avg + baseline-relative stats |
| `Sleep Score/sleep_score.csv` | per sleep | `sleep_score` | single cumulative file, UTC timestamps |

### Loaded from cumulative `Physical Activity_GoogleData/daily_*.csv`

One file per stream covering the whole history — these supersede their
per-day/JSON twins:

| File | → table | Supersedes |
|---|---|---|
| `daily_resting_heart_rate.csv` | `resting_heart_rate` | `Global Export Data/resting_heart_rate-*.json` |
| `daily_heart_rate_variability.csv` | `hrv_daily` | `Heart Rate Variability/Daily Heart Rate Variability Summary - *.csv` |

### Loaded from `Global Export Data/` JSON (no CSV equivalent)

| Files | Grain | → table | Notes |
|---|---|---|---|
| `sleep-YYYY-MM-DD.json` (monthly) | session + stage segments | `sleep_session`, `sleep_stage` | ⚠️ naive local timestamps (unlike intraday JSON!); `levels.data` + `levels.shortData` (short wakes overlap main segments → `is_short`); `type: stages\|classic` have different level vocabularies |
| `sedentary/lightly/moderately/very_active_minutes-*.json` (monthly) | daily | `active_minutes_daily` | four files merged into one row per day |
| `weight-YYYY-MM-DD.json` (monthly) | sparse | `weight` | ⚠️ pounds + naive local date/time; converted to kg |

Example of the classic-JSON shape (synthesized):

```json
[{ "dateTime": "05/01/24 07:00:07", "value": { "bpm": 60, "confidence": 3 } }]
```

### Recognized but intentionally skipped

GPS/location (`gps_location`, `live_pace` — bulky, privacy-sensitive), swim
lengths, `estimated_oxygen_variation`, per-minute `activity_level` and
`sedentary_period` (daily summaries suffice), `time_in_heart_rate_zones`,
daily rollups derivable from loaded intraday data (`daily_respiratory_rate`,
`daily_oxygen_saturation`, `daily_heart_rate_zones`), glucose (empty),
menstrual health, Atrial Fibrillation ECG/PPG, mindfulness,
readiness/stress/VO2max/cardio-load/moods (tier 2 — schema can grow
additively), height, profile, devices, social/commerce/notification metadata,
`Activities/*.tcx`.

The loader lists every skipped file with a reason; nothing is dropped
silently.

## Google Fit export (optional): Basis Peak

Streams under `Takeout/Fit/All Data/` as Google Fit "Data Points" JSON —
epoch-nanosecond timestamps, values in `fitValue[].value.fpVal|intVal`:

```json
{ "Data Points": [ { "dataTypeName": "com.google.heart_rate.bpm",
  "startTimeNanos": 1435983925000000000, "endTimeNanos": 1435983985000000000,
  "fitValue": [ { "value": { "fpVal": 72 } } ] } ] }
```

Only the **raw device streams** are loaded (filename pattern
`raw_<datatype>_com.mybasis.android.basis.peak_Basis_Peak_<serial>_.json`);
`derived_*`/`merge_*` streams and phone-sensor streams are skipped as
duplicates/noise.

| Data type | → table | Notes |
|---|---|---|
| `com.google.heart_rate.bpm` | `heart_rate` | per-minute |
| `com.google.calories.expended` | `calories` | per-minute |
| `com.google.step_count.delta` | `steps` | per-minute |
| `com.google.activity.segment` | *(skipped in v1)* | int activity codes; revisit if wanted |

`Fit/All Sessions/*_SLEEP.json` (UTC session start/end + 'sleep' segments, no
stage vocabulary) load into `sleep_session`; minutes awake are inferred from
gaps between segments. Walking/running session files are skipped in v1.

Loaded with `source = 'googlefit-takeout'`, `device = 'Basis Peak'`.

## Provenance & timestamps

- `source` column: `fitbit-takeout` | `googlefit-takeout` | `api`.
- `device` column: from the CSV `data source` column where present.
- Every stored timestamp is UTC (`timestamptz`). Streams with naive local
  timestamps (AZM, temperature, sleep + weight JSON) are converted using the
  timezone configured for the loader (`TAKEOUT_TZ`, e.g.
  `America/Los_Angeles`) — Takeout does not embed it. If you changed timezones
  over the years, pick the one you lived in most; per-period overrides are not
  supported.
- Weight values follow your Fitbit account unit; set `TAKEOUT_WEIGHT_UNIT`
  (`lbs` default, or `kg`).
- **Concurrent recording devices — never naively sum across `device`.** The
  steps and distance CSVs interleave multiple simultaneous recorders (e.g.
  wearable + "MobileTrack"/"Phone Health Connect" — the phone in your pocket)
  at offset timestamps, so all streams survive the `(time)` natural key.
  Fitbit's app deduplicates these when showing daily totals; the raw export
  does not. A day's total summed across devices can be 2–4× reality. Daily
  aggregates should sum per device first and then take the max (what the
  provisioned dashboard does), or filter to one device.
