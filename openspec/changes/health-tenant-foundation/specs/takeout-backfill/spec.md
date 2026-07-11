# takeout-backfill (delta)

## ADDED Requirements

### Requirement: Full-history load from a Takeout export
The backfill loader SHALL parse a Google Takeout Fitbit export (extracted
directory of per-day/per-category JSON files) and load all supported metric
families into the `health` schema in bulk, without using any rate-limited API.

#### Scenario: Full export loads
- **WHEN** the loader runs against an extracted export with `TAKEOUT_DIR` set
- **THEN** all supported files are ingested and per-table row counts and
  `MIN(time)`/`MAX(time)` are reported for verification against the raw files

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
