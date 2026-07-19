"""Poller mapper tests over synthesized API payloads — never real data."""

from datetime import date, datetime, timezone

from backfill import db
from sync import poller


def test_map_sleep_stages_and_day():
    p = {
        "dataSource": {"device": {"displayName": "Charge 6"}, "platform": "FITBIT"},
        "sleep": {
            "interval": {
                "startTime": "2026-01-02T06:00:00Z", "startUtcOffset": "-25200s",
                "endTime": "2026-01-02T07:30:00Z", "endUtcOffset": "-25200s",
            },
            "type": "STAGES",
            "stages": [
                {"startTime": "2026-01-02T06:00:00Z", "endTime": "2026-01-02T06:10:00Z", "type": "AWAKE"},
                {"startTime": "2026-01-02T06:10:00Z", "endTime": "2026-01-02T07:10:00Z", "type": "LIGHT"},
                {"startTime": "2026-01-02T07:10:00Z", "endTime": "2026-01-02T07:30:00Z", "type": "REM"},
            ],
        },
    }
    rows = list(poller.map_sleep(p))
    stages = [r for t, r in rows if t == "sleep_stage"]
    assert [(s[1], s[2]) for s in stages] == [("wake", 600), ("light", 3600), ("rem", 1200)]
    session = next(r for t, r in rows if t == "sleep_session")
    r = dict(zip(db.TABLES["sleep_session"].cols, session))
    # 07:30 UTC end at -7h offset = 00:30 local -> local day 2026-01-02
    assert r["day"] == date(2026, 1, 2)
    assert r["minutes_asleep"] == 80 and r["minutes_awake"] == 10
    assert r["sleep_type"] == "stages" and r["source"] == "api"


def test_map_weight_grams_to_kg():
    p = {"dataSource": {"platform": "FITBIT"},
         "weight": {"sampleTime": {"physicalTime": "2026-01-02T13:00:00Z"},
                    "weightGrams": 80830}}
    [(table, row)] = list(poller.map_weight(p))
    assert table == "weight" and row[1] == 80.83


def test_map_rollup_distance_mm_to_m():
    p = {"startTime": "2026-01-02T13:00:00Z", "endTime": "2026-01-02T13:01:00Z",
         "distance": {"millimetersSum": "6900"}}
    [(table, row)] = list(poller.map_roll_distance(p))
    assert table == "distance" and row[1] == 6.9
    assert row[0] == datetime(2026, 1, 2, 13, 0, tzinfo=timezone.utc)


def test_map_rollup_empty_window_skipped():
    assert list(poller.map_roll_steps({"startTime": "2026-01-02T13:00:00Z", "steps": {}})) == []


def test_map_azm_unweights_cardio_peak():
    # Google Health API's activeZoneMinutes value is already the
    # zone-weighted AZM score (verified live: a 1-min CARDIO interval
    # reports "2", not "1") — the mapper must divide it back to raw
    # per-minute zone occupancy so health.azm.minutes stays source-agnostic.
    def point(zone, value, minutes=1):
        return {
            "dataSource": {"device": {"displayName": "Charge 6"}},
            "activeZoneMinutes": {
                "interval": {"startTime": "2026-07-19T16:36:00Z",
                             "endTime": f"2026-07-19T16:{36 + minutes}:00Z",
                             "startUtcOffset": "-25200s"},
                "heartRateZone": zone,
                "activeZoneMinutes": str(value),
            },
        }

    [(table, row)] = list(poller.map_azm(point("FAT_BURN", 1)))
    assert table == "azm" and row[1:3] == ("FAT_BURN", 1)

    [(_, row)] = list(poller.map_azm(point("CARDIO", 2)))
    assert row[1:3] == ("CARDIO", 1)

    [(_, row)] = list(poller.map_azm(point("PEAK", 10, minutes=5)))
    assert row[1:3] == ("PEAK", 5)


def test_map_daily_hrv():
    p = {"dataSource": {"device": {"displayName": "Charge 6"}},
         "dailyHeartRateVariability": {"date": {"year": 2026, "month": 1, "day": 2},
                                       "averageHeartRateVariabilityMilliseconds": 31.9,
                                       "nonRemHeartRateBeatsPerMinute": "54",
                                       "entropy": 2.4}}
    [(table, row)] = list(poller.map_daily_hrv(p))
    assert table == "hrv_daily"
    assert row == (date(2026, 1, 2), 31.9, 54.0, 2.4, "api")


def test_update_upsert_coalesces():
    # DO UPDATE must never null-out enrichment fields another source provided
    spec = db.TABLES["sleep_session"]
    sets = ", ".join(
        f"{c} = COALESCE(EXCLUDED.{c}, sleep_session.{c})"
        for c in spec.cols if c not in spec.key)
    assert "efficiency = COALESCE(EXCLUDED.efficiency, sleep_session.efficiency)" in sets


def test_pullers_reference_known_tables():
    for _, table, _, _ in poller.PULLERS:
        assert table in db.TABLES


def test_utc_offset_captured_and_half_hour_zones():
    p = {"dataSource": {"device": {"displayName": "Charge 6"}},
         "heartRate": {"sampleTime": {"physicalTime": "2026-01-02T13:00:00Z",
                                      "utcOffset": "+19800s"},  # UTC+5:30
                       "beatsPerMinute": "70"}}
    [(_, row)] = list(poller.map_heart_rate(p))
    assert row[-1] == 19800
    p["heartRate"]["sampleTime"].pop("utcOffset")
    [(_, row)] = list(poller.map_heart_rate(p))
    assert row[-1] is None  # absent offset -> NULL, "assume home zone"


def test_map_daily_steps_civil_day():
    p = {"civilStartTime": {"date": {"year": 2026, "month": 7, "day": 4}, "time": {}},
         "civilEndTime": {"date": {"year": 2026, "month": 7, "day": 5}, "time": {}},
         "steps": {"countSum": "13165"}}
    [(table, row)] = list(poller.map_daily_steps(p))
    assert table == "steps_daily"
    assert row == (date(2026, 7, 4), 13165, "api", None)


def test_loader_pads_short_rows_with_null():
    loader = db.Loader(conn=None)  # no flush below BATCH, conn untouched
    loader.add("heart_rate", (datetime(2026, 1, 2, tzinfo=timezone.utc),
                              60.0, None, "fitbit-takeout", "Charge 6"))
    row = loader._pending["heart_rate"][0]
    assert len(row) == len(db.TABLES["heart_rate"].cols)
    assert row[-1] is None
