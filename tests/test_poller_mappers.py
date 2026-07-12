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
