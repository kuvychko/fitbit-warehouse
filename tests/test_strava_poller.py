"""Strava poller/auth tests over synthesized payloads — never real data."""

import json
from datetime import datetime, timedelta, timezone

from backfill import db
from sync import strava_authorize, strava_poller


def test_map_activity_units_and_missing_elev_loss():
    a = {"id": 100, "start_date": "2026-07-15T18:08:13Z", "utc_offset": -25200.0,
         "type": "Run", "name": "Morning Run", "distance": 10620.5,
         "moving_time": 4039, "elapsed_time": 4125, "total_elevation_gain": 71.7}
    [(table, row)] = list(strava_poller.map_activity(a))
    r = dict(zip(db.TABLES["strava_activity"].cols, row))
    assert table == "strava_activity"
    assert r["activity_id"] == 100
    assert r["start_time"] == datetime(2026, 7, 15, 18, 8, 13, tzinfo=timezone.utc)
    assert r["utc_offset_s"] == -25200
    assert r["activity_type"] == "Run"
    assert r["distance_m"] == 10620.5           # meters, same unit as the CSV backfill
    assert r["moving_time_s"] == 4039 and r["elapsed_time_s"] == 4125
    assert r["elev_gain_m"] == 71.7
    assert r["elev_loss_m"] is None             # not in summary; COALESCE keeps backfill's
    assert r["source"] == "strava-api"


def test_map_streams_derivation_matches_backfill():
    start = datetime(2026, 7, 15, 18, 8, 13, tzinfo=timezone.utc)
    streams = {"time": {"data": [0, 5, 10]},
               "latlng": {"data": [[37.0, -122.0], [37.001, -122.001], [37.002, -122.002]]},
               "altitude": {"data": [10.0, 11.0, 12.0]}}
    rows = [dict(zip(db.TABLES["activity_track"].cols, r))
            for _, r in strava_poller.map_streams(100, start, streams)]
    # time = start + integer-second offset — identical to the backfill rule,
    # so a re-loaded track shares keys and dedups.
    assert [r["time"] for r in rows] == [start, start + timedelta(seconds=5),
                                         start + timedelta(seconds=10)]
    assert (rows[0]["activity_id"], rows[0]["lat"], rows[0]["lon"], rows[0]["elevation_m"]) \
        == (100, 37.0, -122.0, 10.0)
    assert rows[0]["source"] == "strava-api"


def test_map_streams_tolerates_absent_altitude():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = list(strava_poller.map_streams(
        1, start, {"time": {"data": [0]}, "latlng": {"data": [[1.0, 2.0]]}}))
    assert rows[0][1][4] is None                # elevation NULL when no altitude stream


def test_refresh_rotates_and_persists_before_use(tmp_path, monkeypatch):
    # task 4.3: the stored refresh_token must change after a refresh, and be
    # persisted atomically before the access token is returned to the caller.
    tokfile = tmp_path / "strava_token.json"
    tokfile.write_text(json.dumps({"access_token": "old_a", "refresh_token": "old_r"}))
    monkeypatch.setattr(strava_authorize, "_post_token",
                        lambda payload: {"access_token": "new_a",
                                         "refresh_token": "new_r", "expires_at": 123})
    access = strava_authorize.refresh_access_token("cid", "secret", tokfile)
    assert access == "new_a"
    stored = json.loads(tokfile.read_text())
    assert stored["refresh_token"] == "new_r"   # rotated
    assert stored["access_token"] == "new_a"
    # no leftover temp file from the atomic write
    assert list(tmp_path.glob("*.tmp")) == []


def test_api_base_is_env_driven(monkeypatch):
    monkeypatch.setenv("STRAVA_API_BASE", "https://api-v3.strava.com/")
    assert strava_authorize.api_base() == "https://api-v3.strava.com"
    monkeypatch.delenv("STRAVA_API_BASE", raising=False)
    assert strava_authorize.api_base() == "https://www.strava.com/api/v3"
