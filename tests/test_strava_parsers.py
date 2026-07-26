"""Strava backfill parser tests over synthesized fixtures — never real GPS."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from backfill import db, strava


class Ctx:
    def __init__(self, types):
        self.strava_types = set(types)


def write(tmp_path: Path, rel: str, text: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# activities.csv with the duplicate-column shape that matters: a display
# "Distance" (km/mi) then a meters "Distance"; a display "Elapsed Time" then a
# detailed one. The parser resolves by name + occurrence, so exact positions
# don't need to mirror the real 103-column file.
_HEADER = ("Activity ID,Activity Date,Activity Name,Activity Type,Elapsed Time,"
           "Distance,Filename,Moving Time,Elapsed Time,Distance,Elevation Gain,"
           "Elevation Loss")

_GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1"><metadata><time>2026-07-15T18:08:13Z</time></metadata>
<trk><trkseg>
<trkpt lat="37.0" lon="-122.0"><ele>10.0</ele><time>2026-07-15T18:08:13Z</time></trkpt>
<trkpt lat="37.001" lon="-122.001"><ele>11.5</ele><time>2026-07-15T18:08:18Z</time></trkpt>
</trkseg></trk></gpx>
"""


def test_activity_summary_uses_meters_and_both_times(tmp_path):
    write(tmp_path, "activities/100.gpx", _GPX)
    csv = write(tmp_path, "activities.csv", _HEADER + "\n"
                '100,"Jul 15, 2026, 6:08:13 PM",Morning Run,Run,4125,10.62,'
                "activities/100.gpx,4039.0,4125.0,10620.5,71.7,60.0\n")
    rows = list(strava.parse_activities(csv, Ctx(["Run"])))
    act = [dict(zip(db.TABLES["strava_activity"].cols, r))
           for t, r in rows if t == "strava_activity"]
    assert len(act) == 1
    a = act[0]
    assert a["activity_id"] == 100
    assert a["start_time"] == datetime(2026, 7, 15, 18, 8, 13, tzinfo=timezone.utc)
    assert a["activity_type"] == "Run" and a["name"] == "Morning Run"
    assert a["distance_m"] == 10620.5           # meters (2nd Distance), not 10.62 km
    assert a["moving_time_s"] == 4039           # int from "4039.0"
    assert a["elapsed_time_s"] == 4125
    assert a["elev_gain_m"] == 71.7 and a["elev_loss_m"] == 60.0
    assert a["utc_offset_s"] is None and a["source"] == "strava-export"


def test_track_time_derived_canonically(tmp_path):
    write(tmp_path, "activities/100.gpx", _GPX)
    csv = write(tmp_path, "activities.csv", _HEADER + "\n"
                '100,"Jul 15, 2026, 6:08:13 PM",Run,Run,4125,10.62,'
                "activities/100.gpx,4039.0,4125.0,10620.5,71.7,60.0\n")
    tracks = [dict(zip(db.TABLES["activity_track"].cols, r))
              for t, r in strava.parse_activities(csv, Ctx(["Run"])) if t == "activity_track"]
    start = datetime(2026, 7, 15, 18, 8, 13, tzinfo=timezone.utc)
    assert [t["time"] for t in tracks] == [start, start + timedelta(seconds=5)]
    assert (tracks[0]["lat"], tracks[0]["lon"], tracks[0]["elevation_m"]) == (37.0, -122.0, 10.0)
    assert tracks[1]["elevation_m"] == 11.5 and tracks[0]["source"] == "strava-export"


def test_type_allowlist_skips_and_reports(tmp_path, capsys):
    csv = write(tmp_path, "activities.csv", _HEADER + "\n"
                '1,"Jul 15, 2026, 6:00:00 PM",Ride,Ride,10,1.0,,10.0,10.0,1000.0,0,0\n'
                '2,"Jul 15, 2026, 7:00:00 PM",Run,Run,10,1.0,,10.0,10.0,1000.0,0,0\n')
    rows = list(strava.parse_activities(csv, Ctx(["Run"])))
    ids = [r[0] for t, r in rows if t == "strava_activity"]
    assert ids == [2]                                   # Ride filtered out
    assert "skipped 1 activities by type" in capsys.readouterr().out


def test_missing_gpx_reported_but_summary_loads(tmp_path, capsys):
    csv = write(tmp_path, "activities.csv", _HEADER + "\n"
                '3,"Jul 15, 2026, 8:00:00 PM",Run,Run,10,1.0,'
                "activities/nope.gpx,10.0,10.0,1000.0,0,0\n")
    rows = list(strava.parse_activities(csv, Ctx(["Run"])))
    assert [t for t, _ in rows] == ["strava_activity"]  # summary yes, no track
    assert "no GPX track" in capsys.readouterr().out


def test_strava_tables_registered_with_time_col():
    assert db.time_col(db.TABLES["strava_activity"]) == "start_time"
    assert db.time_col(db.TABLES["activity_track"]) == "time"
    # activity_track keys on (activity_id, time); geog is generated, not a col
    assert db.TABLES["activity_track"].key == ("activity_id", "time")
    assert "geog" not in db.TABLES["activity_track"].cols
