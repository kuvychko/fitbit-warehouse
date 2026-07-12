# takeout-backfill (delta)

## ADDED Requirements

### Requirement: Basis-era daily steps
The Google Fit parser SHALL derive `steps_daily` rows for the Basis era by
summing Basis intraday steps per `TAKEOUT_TZ` civil day
(`source='googlefit-takeout'`, `device='Basis Peak'`). Fitbit-era daily
steps SHALL NOT be derived from the export's intraday CSVs (multi-device
double-count); that history comes from the API bootstrap.

#### Scenario: Basis days present once
- **WHEN** the Google Fit export is loaded and the API bootstrap has run
- **THEN** each 2015 Basis day has exactly one `steps_daily` row and no
  Fitbit-era day has a takeout-derived row
