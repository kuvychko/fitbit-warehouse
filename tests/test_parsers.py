"""Parser tests over synthesized fixtures — never real health data."""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backfill import db, fitbit, googlefit
from backfill.util import classic_utc_ts, opt_float, to_kg, utc_ts


class Ctx:
    tz = ZoneInfo("America/Los_Angeles")
    weight_unit = "lbs"

    def __init__(self):
        self.active_minutes = {}


def stream_parser(streams, name):
    for entry in streams:
        if entry[0] == name:
            return entry[-1]
    raise KeyError(name)


def write(tmp_path: Path, rel: str, text: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --- util ---------------------------------------------------------------------

def test_utc_ts():
    assert utc_ts("2024-05-01T00:00:03Z") == datetime(2024, 5, 1, 0, 0, 3, tzinfo=timezone.utc)


def test_classic_utc_ts_is_utc():
    assert classic_utc_ts("05/01/24 07:00:07") == datetime(2024, 5, 1, 7, 0, 7, tzinfo=timezone.utc)


def test_opt_float_sentinels():
    assert opt_float("") is None
    assert opt_float(None) is None
    assert opt_float("-1.0") is None
    assert opt_float("15.8") == 15.8


def test_to_kg():
    assert to_kg(220.46226218, "lbs") == pytest.approx(100.0)
    assert to_kg(80.0, "kg") == 80.0


# --- Fitbit CSV streams ---------------------------------------------------------

def test_heart_rate_csv(tmp_path):
    p = write(tmp_path, "heart_rate_2024-05-01.csv",
              "timestamp,beats per minute,data source\n"
              "2024-05-01T00:00:03Z,72.0,Charge 5\n"
              "2024-05-01T00:00:08Z,,Charge 5\n")  # empty value skipped
    parser = stream_parser(fitbit.STREAMS, "heart_rate")
    rows = list(parser(p, Ctx()))
    assert rows == [("heart_rate",
                     (datetime(2024, 5, 1, 0, 0, 3, tzinfo=timezone.utc),
                      72.0, None, "fitbit-takeout", "Charge 5"))]


def test_azm_csv_naive_local_to_utc(tmp_path):
    # 09:07 Pacific (PDT, UTC-7) → 16:07 UTC
    p = write(tmp_path, "Active Zone Minutes - 2024-05-01.csv",
              "date_time,heart_zone_id,total_minutes\n"
              "2024-05-01T09:07,FAT_BURN,1\n")
    parser = stream_parser(fitbit.STREAMS, "azm")
    [(table, row)] = list(parser(p, Ctx()))
    assert table == "azm"
    assert row[0] == datetime(2024, 5, 1, 16, 7, tzinfo=timezone.utc)
    assert row[1:3] == ("FAT_BURN", 1)


def test_breathing_rate_sentinel(tmp_path):
    c = " sleep stats - milli breaths per minute"
    header = ("timestamp," + ",".join(
        f"{k}{c},{k} sleep stats - standard deviation milli breaths per minute,"
        f"{k} sleep stats - signal to noise" for k in ("deep", "light", "rem", "full"))
        + ",data source")
    p = write(tmp_path, "respiratory_rate_sleep_summary_2024-05-01.csv",
              header + "\n2024-05-01T06:17:00Z,15.8,1.4,6.7,16.2,2.2,7.6,-1.0,0.0,2.0,15.8,1.4,6.7,Pixel Watch\n")
    [(table, row)] = list(fitbit.parse_breathing(p, Ctx()))
    assert table == "breathing_rate"
    assert row[1] == 15.8 and row[3] is None  # rem -1.0 → None
    assert row[6] == "Pixel Watch"


def test_sleep_json_local_times_and_stages(tmp_path):
    log = [{
        "logId": 1, "dateOfSleep": "2024-06-22",
        "startTime": "2024-06-21T23:30:00.000", "endTime": "2024-06-22T06:30:00.000",
        "duration": 25200000, "minutesToFallAsleep": 5, "minutesAsleep": 380,
        "minutesAwake": 35, "minutesAfterWakeup": 2, "timeInBed": 420,
        "efficiency": 90, "type": "stages", "logType": "auto_detected",
        "mainSleep": True,
        "levels": {
            "summary": {"deep": {"minutes": 60}},
            "data": [{"dateTime": "2024-06-21T23:30:00.000", "level": "light", "seconds": 1800}],
            "shortData": [{"dateTime": "2024-06-22T01:00:00.000", "level": "wake", "seconds": 60}],
        },
    }]
    p = write(tmp_path, "sleep-2024-06-22.json", json.dumps(log))
    rows = list(fitbit.parse_sleep(p, Ctx()))
    session = next(r for t, r in rows if t == "sleep_session")
    # 23:30 PDT = 06:30 UTC next day
    assert session[0] == datetime(2024, 6, 22, 6, 30, tzinfo=timezone.utc)
    assert session[2] == "2024-06-22"
    stages = [(r[1], r[3]) for t, r in rows if t == "sleep_stage"]
    assert stages == [("light", False), ("wake", True)]


def test_weight_json_lbs_to_kg(tmp_path):
    entries = [{"logId": 1, "weight": 220.46226218, "bmi": 25.0,
                "date": "03/14/26", "time": "06:22:41"}]
    p = write(tmp_path, "weight-2026-03-14.json", json.dumps(entries))
    [(table, row)] = list(fitbit.parse_weight(p, Ctx()))
    assert table == "weight"
    assert row[1] == pytest.approx(100.0)
    # 06:22 PDT → 13:22 UTC
    assert row[0] == datetime(2026, 3, 14, 13, 22, 41, tzinfo=timezone.utc)


def test_active_minutes_merge(tmp_path):
    ctx = Ctx()
    for kind, val in (("sedentary", "600"), ("very_active", "45")):
        p = write(tmp_path, f"{kind}_minutes-2024-09-20.json",
                  json.dumps([{"dateTime": "09/20/24 00:00:00", "value": val}]))
        parser = stream_parser(fitbit.STREAMS, f"active_minutes/{kind.split('_')[0]}"
                               if kind != "very_active" else "active_minutes/very")
        list(parser(p, ctx))
    rows = list(fitbit.finish_active_minutes(ctx))
    assert rows == [("active_minutes_daily",
                     (date(2024, 9, 20), 600, None, None, 45, "fitbit-takeout"))]


def test_rhr_daily_csv(tmp_path):
    p = write(tmp_path, "daily_resting_heart_rate.csv",
              "timestamp,beats per minute,data source\n"
              "2016-10-04T00:00:00Z,55.965,Fitbit App\n")
    [(table, row)] = list(fitbit.parse_rhr_daily(p, Ctx()))
    assert (table, row[0], row[1]) == ("resting_heart_rate", date(2016, 10, 4), 55.965)


# --- Google Fit / Basis ----------------------------------------------------------

def test_googlefit_data_points(tmp_path):
    doc = {"Data Points": [
        {"dataTypeName": "com.google.heart_rate.bpm",
         "startTimeNanos": 1435983925000000000, "endTimeNanos": 1435983985000000000,
         "fitValue": [{"value": {"fpVal": 72.0}}]},
        {"dataTypeName": "com.google.heart_rate.bpm",
         "startTimeNanos": 1435983985000000000, "endTimeNanos": 1435984045000000000,
         "fitValue": []},  # empty fitValue skipped
    ]}
    p = write(tmp_path, "raw.json", json.dumps(doc))
    parser = googlefit.make_parser("heart_rate", "fpVal", float)
    [(table, row)] = list(parser(p, Ctx()))
    assert table == "heart_rate"
    assert row[0] == datetime.fromtimestamp(1435983925, tz=timezone.utc)
    assert row[1:] == (72.0, None, "googlefit-takeout", "Basis Peak")


def test_googlefit_sleep_session(tmp_path):
    doc = {"fitnessActivity": "sleep",
           "startTime": "2015-07-03T23:09:00Z", "endTime": "2015-07-04T00:19:00Z",
           "duration": "4200s",
           "segment": [
               {"startTime": "2015-07-03T23:09:00Z", "endTime": "2015-07-03T23:44:00Z"},
               {"startTime": "2015-07-03T23:54:00Z", "endTime": "2015-07-04T00:19:00Z"},
           ]}
    p = write(tmp_path, "2015-07-03T16_09_00-07_00_SLEEP.json", json.dumps(doc))
    [(table, row)] = list(googlefit.parse_sleep_session(p, Ctx()))
    assert table == "sleep_session"
    spec = db.TABLES["sleep_session"]
    r = dict(zip(spec.cols, row))
    assert r["start_time"] == datetime(2015, 7, 3, 23, 9, tzinfo=timezone.utc)
    assert r["day"] == date(2015, 7, 3)  # local (Pacific) date of sleep start
    assert r["minutes_asleep"] == 60 and r["minutes_awake"] == 10
    assert r["device"] == "Basis Peak"


# --- Classification (loud handling of unknown input) ---------------------------

def test_classify_unknown_reported(tmp_path):
    from backfill.__main__ import classify
    write(tmp_path, "Google Health/Physical Activity_GoogleData/steps_2024-05-01.csv",
          "timestamp,steps,data source\n")
    write(tmp_path, "Google Health/Global Export Data/steps-2024-05-01.json", "[]")
    write(tmp_path, "Google Health/Novel Feature/mystery.csv", "a,b\n")
    matched, skipped, unknown = classify(tmp_path, fitbit.STREAMS, fitbit.SKIP_PATTERNS)
    assert [p.name for p in matched["steps"]] == ["steps_2024-05-01.csv"]
    assert skipped["classic-JSON twin of a loaded CSV stream"] == 1
    assert unknown == ["Google Health/Novel Feature/mystery.csv"]


def test_all_load_tables_exist_in_registry():
    # every table parsers can emit must be in db.TABLES
    emitted = {"heart_rate", "steps", "calories", "distance", "floors", "spo2",
               "hrv", "device_temperature", "azm", "sleep_session", "sleep_stage",
               "sleep_score", "breathing_rate", "nightly_temperature",
               "resting_heart_rate", "active_minutes_daily", "hrv_daily",
               "weight", "body_fat"}
    assert emitted == set(db.TABLES)
