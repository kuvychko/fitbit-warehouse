# takeout-backfill (delta)

## ADDED Requirements

### Requirement: Full-history load from a Takeout export
The backfill loader SHALL parse a Google Takeout Fitbit ("Google Health")
export — an extracted directory of per-day/per-category CSV and JSON files —
and load all supported metric families into the `health` schema in bulk,
without using any rate-limited API. Where a stream exists in both export
formats, the loader SHALL treat the newer CSVs
("Physical Activity_GoogleData"; ISO-8601 UTC timestamps, device source
column) as the source of truth and SHALL NOT double-load the classic JSON
("Global Export Data") duplicate; classic JSON is used only for streams with
no CSV equivalent (e.g. sleep stage `levels`, exercise logs), accounting for
its quirk that timestamp values are UTC while files are bounded by local day.

#### Scenario: Full export loads
- **WHEN** the loader runs against an extracted export with `TAKEOUT_DIR` set
- **THEN** all supported files are ingested and per-table row counts and
  `MIN(time)`/`MAX(time)` are reported for verification against the raw files

#### Scenario: Dual-format streams load once
- **WHEN** a metric stream is present in both the CSV and classic JSON formats
- **THEN** rows are loaded from the CSVs only and the JSON duplicates are
  reported as intentionally skipped

### Requirement: Google Fit (Basis Peak) export support
The loader SHALL also parse a Google Takeout **Google Fit** export ("All Data"
Data-Points JSON with nanosecond epoch timestamps) and load the Basis Peak
raw streams — per-minute heart rate, calories expended, steps, and activity
segments — into the same hypertables with `source = 'googlefit-takeout'` and
`device = 'Basis Peak'`.

#### Scenario: Basis history loads
- **WHEN** the loader runs against an extracted Google Fit export
- **THEN** the Basis Peak heart-rate/calories/steps/activity points land in
  the same tables as Fitbit data, distinguishable by `source` and `device`,
  and derived/merged Google Fit streams are reported as intentionally skipped

### Requirement: Idempotent, resumable loading
The loader SHALL be safe to re-run over the same export (and over an export
overlapping previously synced API data): already-present rows are absorbed by
the natural-key upsert, never duplicated.

#### Scenario: Second run adds nothing
- **WHEN** the loader runs twice over the same export
- **THEN** row counts after the second run equal counts after the first

### Requirement: Loud handling of unknown input
The loader SHALL validate each file before loading, skip files it does not
recognize, and report every skipped or partially parsed file explicitly —
never silently dropping data.

#### Scenario: Unknown file reported
- **WHEN** the export contains a file the loader does not support
- **THEN** the run completes for supported files and the summary lists the
  unsupported file(s) and why they were skipped
