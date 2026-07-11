"""Parsers for the Fitbit ("Google Health") Takeout export.

Stream inventory and format quirks are documented in docs/takeout-format.md.
CSV-first rule: streams that exist in both export formats are read from the
newer CSVs; their classic-JSON twins appear in SKIP_PATTERNS.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterator
from pathlib import Path

from psycopg.types.json import Jsonb

from .util import classic_date, local_ts, opt_float, opt_int, to_kg, utc_ts

SOURCE = "fitbit-takeout"

PA = r"Physical Activity_GoogleData/"
GE = r"Global Export Data/"
D = r"\d{4}-\d{2}-\d{2}"


def _csv_rows(path: Path) -> Iterator[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


def _dev(row: dict) -> str | None:
    return row.get("data source") or None


# --- Generic per-sample CSV streams (timestamp,value[,data source]) ----------

def _simple_csv(value_col: str, cast, ts_mode: str = "utc"):
    """Parser factory for one-value-per-row CSV streams."""

    def parse(path: Path, ctx) -> Iterator[tuple[str, tuple]]:
        for row in _csv_rows(path):
            ts_raw = row.get("timestamp") or row.get("date_time") or row.get("recorded_time")
            if not ts_raw or row.get(value_col) in (None, ""):
                continue
            ts = utc_ts(ts_raw) if ts_mode == "utc" else local_ts(ts_raw, ctx.tz)
            yield cast(ts, row)

    return parse


def _hr(ts, row):
    return ("heart_rate", (ts, float(row["beats per minute"]), None, SOURCE, _dev(row)))

def _steps(ts, row):
    return ("steps", (ts, int(float(row["steps"])), SOURCE, _dev(row)))

def _calories(ts, row):
    return ("calories", (ts, float(row["calories"]), SOURCE, _dev(row)))

def _distance(ts, row):
    return ("distance", (ts, float(row["distance"]), SOURCE, _dev(row)))

def _floors(ts, row):
    return ("floors", (ts, int(float(row["floors"])), SOURCE, _dev(row)))

def _spo2(ts, row):
    return ("spo2", (ts, float(row["oxygen saturation percentage"]), SOURCE, _dev(row)))

def _hrv(ts, row):
    return ("hrv", (ts, float(row["root mean square of successive differences milliseconds"]),
                    opt_float(row.get("standard deviation milliseconds")), SOURCE, _dev(row)))

def _azm(ts, row):
    return ("azm", (ts, row["heart_zone_id"].strip(), int(row["total_minutes"]), SOURCE, None))

def _device_temp(ts, row):
    return ("device_temperature", (ts, float(row["temperature"]),
                                   row.get("sensor_type") or None, SOURCE, None))

def _body_fat(ts, row):
    return ("body_fat", (ts, float(row["body fat percentage"]), SOURCE, _dev(row)))


# --- Multi-column / irregular CSV streams -------------------------------------

def parse_breathing(path: Path, ctx) -> Iterator[tuple[str, tuple]]:
    # Header says "milli breaths per minute" but values are plain breaths/min;
    # -1.0 marks missing stages.
    c = " sleep stats - milli breaths per minute"
    for row in _csv_rows(path):
        if not row.get("timestamp"):
            continue
        yield ("breathing_rate", (utc_ts(row["timestamp"]),
                                  opt_float(row.get("deep" + c)), opt_float(row.get("light" + c)),
                                  opt_float(row.get("rem" + c)), opt_float(row.get("full" + c)),
                                  SOURCE, _dev(row)))


def parse_nightly_temp(path: Path, ctx) -> Iterator[tuple[str, tuple]]:
    for row in _csv_rows(path):
        if not row.get("sleep_start"):
            continue
        yield ("nightly_temperature", (
            local_ts(row["sleep_start"], ctx.tz),
            local_ts(row["sleep_end"], ctx.tz) if row.get("sleep_end") else None,
            opt_float(row.get("nightly_temperature")),
            opt_int(row.get("temperature_samples")),
            opt_float(row.get("baseline_relative_sample_standard_deviation")),
            SOURCE, None))


def parse_hrv_daily(path: Path, ctx) -> Iterator[tuple[str, tuple]]:
    # cumulative daily_heart_rate_variability.csv; midnight-UTC daily stamps
    for row in _csv_rows(path):
        if not row.get("timestamp"):
            continue
        yield ("hrv_daily", (utc_ts(row["timestamp"]).date(),
                             opt_float(row.get("average heart rate variability milliseconds")),
                             opt_float(row.get("non rem heart rate beats per minute")),
                             opt_float(row.get("entropy")), SOURCE))


def parse_rhr_daily(path: Path, ctx) -> Iterator[tuple[str, tuple]]:
    # cumulative daily_resting_heart_rate.csv; midnight-UTC daily stamps
    for row in _csv_rows(path):
        if not row.get("timestamp") or not row.get("beats per minute"):
            continue
        yield ("resting_heart_rate", (utc_ts(row["timestamp"]).date(),
                                      float(row["beats per minute"]), None, SOURCE))


def parse_sleep_score(path: Path, ctx) -> Iterator[tuple[str, tuple]]:
    for row in _csv_rows(path):
        if not row.get("timestamp"):
            continue
        yield ("sleep_score", (utc_ts(row["timestamp"]), opt_int(row.get("sleep_log_entry_id")),
                               opt_int(row.get("overall_score")), opt_int(row.get("composition_score")),
                               opt_int(row.get("revitalization_score")), opt_int(row.get("duration_score")),
                               opt_int(row.get("deep_sleep_in_minutes")), opt_float(row.get("resting_heart_rate")),
                               opt_float(row.get("restlessness")), SOURCE))


# --- Classic JSON streams ------------------------------------------------------

def parse_sleep(path: Path, ctx) -> Iterator[tuple[str, tuple]]:
    logs = json.loads(path.read_text(encoding="utf-8"))
    for log in logs:
        start = local_ts(log["startTime"], ctx.tz)
        levels = log.get("levels") or {}
        yield ("sleep_session", (
            start, local_ts(log["endTime"], ctx.tz), log["dateOfSleep"],
            log.get("logId"), log.get("duration"), log.get("minutesAsleep"),
            log.get("minutesAwake"), log.get("minutesToFallAsleep"),
            log.get("minutesAfterWakeup"), log.get("timeInBed"), log.get("efficiency"),
            log.get("type"), log.get("logType"), log.get("mainSleep"),
            Jsonb(levels.get("summary")) if levels.get("summary") else None,
            SOURCE, None))
        for seg, is_short in ((levels.get("data"), False), (levels.get("shortData"), True)):
            for s in seg or []:
                yield ("sleep_stage", (local_ts(s["dateTime"], ctx.tz), s["level"],
                                       int(s["seconds"]), is_short, SOURCE))


def parse_weight(path: Path, ctx) -> Iterator[tuple[str, tuple]]:
    for entry in json.loads(path.read_text(encoding="utf-8")):
        if entry.get("weight") in (None, 0):
            continue
        ts = local_ts(
            f"{classic_date(entry['date']).isoformat()}T{entry.get('time', '00:00:00')}",
            ctx.tz)
        yield ("weight", (ts, to_kg(float(entry["weight"]), ctx.weight_unit),
                          opt_float(entry.get("bmi")), SOURCE, None))


# active-minutes families are merged into one daily row; the runner collects
# partial values here and flushes them via finish_active_minutes().
_AM_COL = {"sedentary": 0, "lightly_active": 1, "moderately_active": 2, "very_active": 3}


def parse_active_minutes(kind: str):
    def parse(path: Path, ctx) -> Iterator[tuple[str, tuple]]:
        for entry in json.loads(path.read_text(encoding="utf-8")):
            day = classic_date(entry["dateTime"])
            ctx.active_minutes.setdefault(day, [None] * 4)[_AM_COL[kind]] = int(entry["value"])
        return
        yield  # make this a generator: rows are emitted by finish_active_minutes

    return parse


def finish_active_minutes(ctx) -> Iterator[tuple[str, tuple]]:
    for day, vals in sorted(ctx.active_minutes.items()):
        yield ("active_minutes_daily", (day, *vals, SOURCE))


# --- Stream registry -----------------------------------------------------------

# (name, filename regex, parser). First match wins.
STREAMS: list[tuple[str, re.Pattern, object]] = [
    ("heart_rate", re.compile(PA + rf"heart_rate_{D}\.csv$"), _simple_csv("beats per minute", _hr)),
    ("steps", re.compile(PA + rf"steps_{D}\.csv$"), _simple_csv("steps", _steps)),
    ("calories", re.compile(PA + rf"calories_{D}\.csv$"), _simple_csv("calories", _calories)),
    ("distance", re.compile(PA + rf"distance_{D}\.csv$"), _simple_csv("distance", _distance)),
    ("floors", re.compile(PA + rf"floors_{D}\.csv$"), _simple_csv("floors", _floors)),
    ("spo2", re.compile(PA + rf"oxygen_saturation_{D}\.csv$"), _simple_csv("oxygen saturation percentage", _spo2)),
    ("hrv", re.compile(PA + rf"heart_rate_variability_{D}\.csv$"),
     _simple_csv("root mean square of successive differences milliseconds", _hrv)),
    ("breathing_rate", re.compile(PA + rf"respiratory_rate_sleep_summary_{D}\.csv$"), parse_breathing),
    ("body_fat", re.compile(PA + rf"body_fat_{D}\.csv$"), _simple_csv("body fat percentage", _body_fat)),
    ("azm", re.compile(rf"Active Zone Minutes \(AZM\)/Active Zone Minutes - {D}\.csv$"),
     _simple_csv("total_minutes", _azm, ts_mode="local")),
    ("device_temperature", re.compile(rf"Temperature/Device Temperature - {D}\.csv$"),
     _simple_csv("temperature", _device_temp, ts_mode="local")),
    ("nightly_temperature", re.compile(rf"Temperature/Computed Temperature - {D}\.csv$"), parse_nightly_temp),
    ("hrv_daily", re.compile(PA + r"daily_heart_rate_variability\.csv$"), parse_hrv_daily),
    ("resting_heart_rate", re.compile(PA + r"daily_resting_heart_rate\.csv$"), parse_rhr_daily),
    ("sleep_score", re.compile(r"Sleep Score/sleep_score\.csv$"), parse_sleep_score),
    ("sleep", re.compile(GE + rf"sleep-{D}\.json$"), parse_sleep),
    ("weight", re.compile(GE + rf"weight-{D}\.json$"), parse_weight),
    ("active_minutes/sedentary", re.compile(GE + rf"sedentary_minutes-{D}\.json$"), parse_active_minutes("sedentary")),
    ("active_minutes/lightly", re.compile(GE + rf"lightly_active_minutes-{D}\.json$"), parse_active_minutes("lightly_active")),
    ("active_minutes/moderately", re.compile(GE + rf"moderately_active_minutes-{D}\.json$"), parse_active_minutes("moderately_active")),
    ("active_minutes/very", re.compile(GE + rf"very_active_minutes-{D}\.json$"), parse_active_minutes("very_active")),
]

# Recognized-and-skipped files: (regex, reason). Checked after STREAMS.
SKIP_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(GE + r"(heart_rate|steps|calories|distance|altitude|resting_heart_rate)-"), "classic-JSON twin of a loaded CSV stream"),
    (re.compile(r"Heart Rate Variability/Daily Heart Rate Variability Summary"), "per-day twin of cumulative daily_heart_rate_variability.csv"),
    (re.compile(PA + r"(daily_respiratory_rate|daily_oxygen_saturation|daily_heart_rate_zones|daily_vo2_max|cardio_acute_chronic_workload_ratio|height|moods)\.csv$"), "daily rollup/tier-2 stream, not imported in v1"),
    (re.compile(GE + r"(estimated_oxygen_variation|time_in_heart_rate_zones|swim_lengths_data)-"), "not imported (see docs/takeout-format.md)"),
    (re.compile(GE + r"(exercise|run_vo2_max|demographic_vo2_max|height|badge)"), "tier-2 stream, not imported in v1"),
    (re.compile(PA + r"(gps_location|live_pace)"), "GPS/pace not imported (bulky, privacy-sensitive)"),
    (re.compile(PA + r"(sedentary_period|activity_level|micro_stillness)"), "daily summaries imported instead"),
    (re.compile(PA + r"(time_in_heart_rate_zone|calories_in_heart_rate_zone|active_zone_minutes|active_minutes|active_energy_burned)"), "not imported (see docs/takeout-format.md)"),
    (re.compile(PA + r"(swim_lengths_data|altitude|speed|vo2_max|weight|demographic_vo2max|run_vo2max|cardio_load|mindfulness_session|daily_readiness|body_temperature|daily_sleep_temperature_derivations|oxygen_saturation_readme|heart_rate_readme)"), "not imported or duplicate of another stream"),
    (re.compile(r"Oxygen Saturation \(SpO2\)/"), "duplicate of oxygen_saturation CSVs (no device column)"),
    (re.compile(r"Heart Rate Variability/(Heart Rate Variability (Details|Histogram)|Daily Respiratory Rate Summary|Respiratory Rate Summary)"), "duplicate/derived HRV variant (naive timestamps)"),
    (re.compile(r"(Daily Readiness|Stress Score|Stress Journal|Mindfulness|Atrial Fibrillation|Biometrics|Menstrual Health|Snore and Noise Detect|Guided Programs|Fitbit Premium|Fitbit Friends|Discover|Sleep Profile|Sleep/)"), "tier-2 or n/a category, not imported in v1"),
    (re.compile(r"(Your Profile|Paired Devices|User Security Data|Account Changes|InAppNotifications|Email Notifications|Social|Commerce|Activity Goals|Health Fitness Data_GoogleData|Heart Rate/)"), "metadata/app category, not health time series"),
    (re.compile(r"readme.*\.txt$", re.IGNORECASE), "readme"),
    (re.compile(r"\.txt$"), "readme/notes file"),
    (re.compile(r"\.(png|jpg|pdf)$", re.IGNORECASE), "media file"),
]
