"""Google Health API sync poller.

    python -m sync.poller [--once]

Every POLL_INTERVAL seconds (default 7200 = 2 h; dashboards are not a
real-time artifact) pulls each configured data type from the Google Health
API and upserts into the `health` schema.

Catch-up is DB-driven: per table the window starts at
max(latest stored point - OVERLAP_HOURS, now - CATCHUP_CAP_DAYS) — the
warehouse itself is the cursor, so missed cycles self-heal and revised
values within the overlap get re-read (COALESCE upserts absorb them).

Rate limits (429) end the cycle gracefully; the next cycle's window covers
the gap. A Healthchecks ping fires only after a fully successful cycle.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backfill import db
from sync.authorize import api_get, api_post, load_dotenv, refresh_access_token, token_path

SOURCE = "api"
WEARABLES = "users/me/dataSourceFamilies/google-wearables"


# --- payload helpers ----------------------------------------------------------

def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def offset_s(s: str | None) -> int:
    return int(s.rstrip("s")) if s else 0


def opt_offset(s: str | None) -> int | None:
    """utcOffset like "-25200s" -> seconds; None when the API omits it."""
    return int(s.rstrip("s")) if s else None


def civil(d: dict) -> date:
    return date(d["year"], d["month"], d["day"])


def device_of(point: dict) -> str | None:
    return ((point.get("dataSource") or {}).get("device") or {}).get("displayName")


class CycleAbort(Exception):
    """Rate limit / auth trouble: end this cycle, next one catches up."""


# --- pull strategies ------------------------------------------------------------

def pull_list(access, data_type, member, window, mapper):
    """GET dataPoints with an AIP-160 physical-time filter, paginated."""
    lo, hi = window
    flt = (f'{member} >= "{lo.strftime("%Y-%m-%dT%H:%M:%SZ")}" '
           f'AND {member} < "{hi.strftime("%Y-%m-%dT%H:%M:%SZ")}"')
    params = {"filter": flt, "pageSize": 10000}
    while True:
        status, body = api_get(access, f"/users/me/dataTypes/{data_type}/dataPoints", params)
        _check(status, data_type, body)
        doc = json.loads(body)
        for point in doc.get("dataPoints", []):
            yield from mapper(point)
        token = doc.get("nextPageToken")
        if not token:
            return
        params["pageToken"] = token


def pull_daily_list(access, data_type, member, days, mapper):
    """Daily-summary types filter on a civil date."""
    flt = f'{member} >= "{days[0].isoformat()}"'
    params = {"filter": flt, "pageSize": 10000}
    while True:
        status, body = api_get(access, f"/users/me/dataTypes/{data_type}/dataPoints", params)
        _check(status, data_type, body)
        doc = json.loads(body)
        for point in doc.get("dataPoints", []):
            yield from mapper(point)
        token = doc.get("nextPageToken")
        if not token:
            return
        params["pageToken"] = token


def pull_rollup(access, data_type, window, mapper, chunk_days=5):
    """POST :rollUp at 60 s windows, wearables family (pre-deduplicated),
    chunked to stay under the 14-day range cap."""
    lo, hi = window
    while lo < hi:
        mid = min(lo + timedelta(days=chunk_days), hi)
        body = {"range": {"startTime": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "endTime": mid.strftime("%Y-%m-%dT%H:%M:%SZ")},
                "windowSize": "60s", "dataSourceFamily": WEARABLES,
                "pageSize": 10000}
        while True:
            status, resp = api_post(access, f"/users/me/dataTypes/{data_type}/dataPoints:rollUp", body)
            _check(status, data_type, resp)
            doc = json.loads(resp)
            for point in doc.get("rollupDataPoints", []):
                yield from mapper(point)
            token = doc.get("nextPageToken")
            if not token:
                break
            body["pageToken"] = token
        lo = mid


def pull_daily_rollup(access, data_type, day_range, mapper, chunk_days=90):
    """POST :dailyRollUp over civil-date chunks (Fitbit-deduplicated,
    travel-aware civil days). end is exclusive. 90-day chunks: the API 400s
    (INVALID_ROLLUP_QUERY_DURATION) somewhere between 90 and 180 days."""
    civil_d = lambda d: {"year": d.year, "month": d.month, "day": d.day}
    lo, hi = day_range
    while lo < hi:
        mid = min(lo + timedelta(days=chunk_days), hi)
        # NB: no pageSize — dailyRollUp 400s (INVALID_ROLLUP_QUERY_DURATION)
        # when pageSize exceeds what the duration allows; the default page
        # covers our chunks and nextPageToken handles the rest.
        body = {"range": {"start": {"date": civil_d(lo)}, "end": {"date": civil_d(mid)}}}
        while True:
            status, resp = api_post(
                access, f"/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp", body)
            _check(status, data_type, resp)
            doc = json.loads(resp)
            for point in doc.get("rollupDataPoints", []):
                yield from mapper(point)
            token = doc.get("nextPageToken")
            if not token:
                break
            body["pageToken"] = token
        lo = mid


def _check(status, data_type, body):
    if status == 429:
        raise CycleAbort(f"429 rate-limited on {data_type}")
    if status != 200:
        raise CycleAbort(f"HTTP {status} on {data_type}: {body[:300]}")


# --- mappers: API payload -> table rows (column order per backfill.db.TABLES) ---

def map_heart_rate(p):
    hr = p["heartRate"]
    yield ("heart_rate", (ts(hr["sampleTime"]["physicalTime"]), float(hr["beatsPerMinute"]),
                          None, SOURCE, device_of(p),
                          opt_offset(hr["sampleTime"].get("utcOffset"))))

def map_spo2(p):
    o = p["oxygenSaturation"]
    yield ("spo2", (ts(o["sampleTime"]["physicalTime"]), float(o["percentage"]),
                    SOURCE, device_of(p), opt_offset(o["sampleTime"].get("utcOffset"))))

def map_hrv(p):
    h = p["heartRateVariability"]
    yield ("hrv", (ts(h["sampleTime"]["physicalTime"]),
                   float(h["rootMeanSquareOfSuccessiveDifferencesMilliseconds"]),
                   None, SOURCE, device_of(p), opt_offset(h["sampleTime"].get("utcOffset"))))

def map_weight(p):
    w = p["weight"]
    yield ("weight", (ts(w["sampleTime"]["physicalTime"]), float(w["weightGrams"]) / 1000.0,
                      None, SOURCE, device_of(p), opt_offset(w["sampleTime"].get("utcOffset"))))

def map_body_fat(p):
    b = p["bodyFat"]
    yield ("body_fat", (ts(b["sampleTime"]["physicalTime"]), float(b["percentage"]),
                        SOURCE, device_of(p), opt_offset(b["sampleTime"].get("utcOffset"))))

# Google Health API's activeZoneMinutes value is already the officially
# weighted AZM score (a 1-minute CARDIO interval reports "2", not "1"), with
# no raw-minutes or multiplier field alongside it (verified live, see
# docs/health-api-notes.md). health.azm.minutes must stay raw regardless of
# source — the Grafana dashboards apply this same CARDIO/PEAK weighting
# themselves — so divide it back out here before storing.
_AZM_WEIGHT = {"FAT_BURN": 1, "CARDIO": 2, "PEAK": 2}

def map_azm(p):
    a = p["activeZoneMinutes"]
    zone = a["heartRateZone"]
    raw_minutes = int(a["activeZoneMinutes"]) // _AZM_WEIGHT[zone]
    yield ("azm", (ts(a["interval"]["startTime"]), zone,
                   raw_minutes, SOURCE, device_of(p),
                   opt_offset(a["interval"].get("startUtcOffset"))))

_STAGE = {"AWAKE": "wake", "LIGHT": "light", "DEEP": "deep", "REM": "rem",
          "ASLEEP": "asleep", "RESTLESS": "restless"}
_ASLEEP = {"light", "deep", "rem", "asleep"}

def map_sleep(p):
    s = p["sleep"]
    iv = s["interval"]
    start, end = ts(iv["startTime"]), ts(iv["endTime"])
    day = (end + timedelta(seconds=offset_s(iv.get("endUtcOffset")))).date()
    stages = s.get("stages", [])
    asleep_s = awake_s = 0
    for st in stages:
        secs = (ts(st["endTime"]) - ts(st["startTime"])).total_seconds()
        level = _STAGE.get(st["type"], st["type"].lower())
        if level in _ASLEEP:
            asleep_s += secs
        else:
            awake_s += secs
        yield ("sleep_stage", (ts(st["startTime"]), level, int(secs), False, SOURCE,
                               opt_offset(st.get("startUtcOffset") or iv.get("startUtcOffset"))))
    dur_ms = int((end - start).total_seconds() * 1000)
    yield ("sleep_session", (
        start, end, day, None, dur_ms,
        round(asleep_s / 60) if stages else None,
        round(awake_s / 60) if stages else None,
        None, None, round(dur_ms / 60000), None,
        s.get("type", "").lower() or None, None, None, None,
        SOURCE, device_of(p), opt_offset(iv.get("startUtcOffset"))))

def map_daily_rhr(p):
    d = p["dailyRestingHeartRate"]
    yield ("resting_heart_rate", (civil(d["date"]), float(d["beatsPerMinute"]), None, SOURCE))

def map_daily_hrv(p):
    d = p["dailyHeartRateVariability"]
    yield ("hrv_daily", (civil(d["date"]),
                         d.get("averageHeartRateVariabilityMilliseconds"),
                         float(d["nonRemHeartRateBeatsPerMinute"]) if d.get("nonRemHeartRateBeatsPerMinute") else None,
                         d.get("entropy"), SOURCE))

def map_daily_steps(p):
    v = (p.get("steps") or {}).get("countSum")
    if v is not None:
        yield ("steps_daily", (civil(p["civilStartTime"]["date"]), int(v), SOURCE, None))

def map_roll_steps(p):
    v = (p.get("steps") or {}).get("countSum")
    if v is not None:
        yield ("steps", (ts(p["startTime"]), int(v), SOURCE, None))

def map_roll_calories(p):
    v = (p.get("totalCalories") or {}).get("kcalSum")
    if v is not None:
        yield ("calories", (ts(p["startTime"]), float(v), SOURCE, None))

def map_roll_distance(p):
    v = (p.get("distance") or {}).get("millimetersSum")
    if v is not None:
        yield ("distance", (ts(p["startTime"]), float(v) / 1000.0, SOURCE, None))

def map_roll_floors(p):
    v = (p.get("floors") or {}).get("floorsSum")
    if v is not None:
        yield ("floors", (ts(p["startTime"]), int(float(v)), SOURCE, None))


# (name, table for the cursor, strategy, args)
PULLERS = [
    ("heart-rate", "heart_rate", "list", ("heart_rate.sample_time.physical_time", map_heart_rate)),
    ("oxygen-saturation", "spo2", "list", ("oxygen_saturation.sample_time.physical_time", map_spo2)),
    ("heart-rate-variability", "hrv", "list", ("heart_rate_variability.sample_time.physical_time", map_hrv)),
    ("weight", "weight", "list", ("weight.sample_time.physical_time", map_weight)),
    ("body-fat", "body_fat", "list", ("body_fat.sample_time.physical_time", map_body_fat)),
    ("active-zone-minutes", "azm", "list", ("active_zone_minutes.interval.start_time", map_azm)),
    ("sleep", "sleep_session", "list", ("sleep.interval.end_time", map_sleep)),
    ("steps", "steps", "rollup", (map_roll_steps,)),
    ("total-calories", "calories", "rollup", (map_roll_calories,)),
    ("distance", "distance", "rollup", (map_roll_distance,)),
    ("floors", "floors", "rollup", (map_roll_floors,)),
    ("steps@daily", "steps_daily", "dailyrollup", (map_daily_steps,)),
    ("daily-resting-heart-rate", "resting_heart_rate", "daily", ("daily_resting_heart_rate.date", map_daily_rhr)),
    ("daily-heart-rate-variability", "hrv_daily", "daily", ("daily_heart_rate_variability.date", map_daily_hrv)),
]


def window_for(conn, table: str, overlap_h: int, cap_days: int):
    """DB-driven catch-up window: from the latest stored point (minus overlap),
    never further back than the cap, up to now."""
    spec = db.TABLES[table]
    tc = db.time_col(spec)
    with conn.cursor() as cur:
        cur.execute(f"SELECT max({tc}) FROM health.{table}")
        (last,) = cur.fetchone()
    now = datetime.now(timezone.utc)
    floor = now - timedelta(days=cap_days)
    if last is None:
        return floor, now
    if isinstance(last, date) and not isinstance(last, datetime):
        last = datetime(last.year, last.month, last.day, tzinfo=timezone.utc)
    return max(last - timedelta(hours=overlap_h), floor), now


def ping_healthchecks(ok: bool):
    url = os.environ.get("HEALTHCHECKS_PING_URL", "").strip()
    if not url:
        return
    target = url if ok else url.rstrip("/") + "/fail"
    try:
        urllib.request.urlopen(target, timeout=10)
    except OSError as e:
        print(f"WARNING: healthchecks ping failed: {e}", file=sys.stderr)


def run_cycle(conn) -> None:
    overlap_h = int(os.environ.get("OVERLAP_HOURS", "24"))
    cap_days = int(os.environ.get("CATCHUP_CAP_DAYS", "30"))
    access = refresh_access_token(
        os.environ["GOOGLE_CLIENT_ID"], os.environ["GOOGLE_CLIENT_SECRET"],
        json.loads(token_path().read_text())["refresh_token"])
    loader = db.Loader(conn)
    for data_type, table, kind, args in PULLERS:
        window = window_for(conn, table, overlap_h, cap_days)
        if kind == "list":
            member, mapper = args
            rows = pull_list(access, data_type, member, window, mapper)
        elif kind == "rollup":
            (mapper,) = args
            rows = pull_rollup(access, data_type, window, mapper)
        elif kind == "dailyrollup":
            (mapper,) = args
            rows = pull_daily_rollup(access, data_type.split("@")[0],
                                     (window[0].date(), window[1].date() + timedelta(days=1)),
                                     mapper)
        else:
            member, mapper = args
            rows = pull_daily_list(access, data_type, member,
                                   (window[0].date(), window[1].date()), mapper)
        n = 0
        for trow in rows:
            loader.add(*trow)
            n += 1
        print(f"  {data_type:<28} window {window[0]:%Y-%m-%d %H:%M} -> {n} rows parsed")
    loader.flush()
    for table, n in sorted(loader.written.items()):
        print(f"  written: {n:>8}  {table}")


def bootstrap_steps_daily(conn, access) -> None:
    """One-time deep dailyRollUp walk: Fitbit era (2016-10-01) -> tomorrow."""
    from datetime import date as _date
    start = _date.fromisoformat(os.environ.get("BOOTSTRAP_START", "2016-10-01"))
    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    loader = db.Loader(conn)
    n = 0
    for trow in pull_daily_rollup(access, "steps", (start, end), map_daily_steps):
        loader.add(*trow)
        n += 1
    loader.flush()
    print(f"  steps_daily bootstrap: {n} days parsed, "
          f"{loader.written.get('steps_daily', 0)} rows written")


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="single cycle, then exit")
    ap.add_argument("--bootstrap-steps-daily", action="store_true",
                    help="one-time deep dailyRollUp walk (Fitbit era -> now) "
                         "filling steps_daily, then exit")
    args = ap.parse_args()
    interval = int(os.environ.get("POLL_INTERVAL", "7200"))

    missing = [v for v in ("HEALTH_RW_PW", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
               if not os.environ.get(v)]
    if missing:
        print(f"ERROR: missing required env: {', '.join(missing)} (see .env.example)",
              file=sys.stderr)
        return 2
    if not token_path().is_file():
        print(f"ERROR: no token at {token_path()} — run `python -m sync.authorize` once",
              file=sys.stderr)
        return 2

    if args.bootstrap_steps_daily:
        access = refresh_access_token(
            os.environ["GOOGLE_CLIENT_ID"], os.environ["GOOGLE_CLIENT_SECRET"],
            json.loads(token_path().read_text())["refresh_token"])
        conn = db.connect()
        try:
            bootstrap_steps_daily(conn, access)
        finally:
            conn.close()
        return 0

    while True:
        started = datetime.now(timezone.utc)
        print(f"=== cycle start {started:%Y-%m-%d %H:%M:%S}Z")
        try:
            conn = db.connect()
            try:
                run_cycle(conn)
            finally:
                conn.close()
            ping_healthchecks(True)
            print("=== cycle OK")
            status = 0
        except CycleAbort as e:
            print(f"=== cycle aborted: {e} (next cycle catches up)", file=sys.stderr)
            ping_healthchecks(False)
            status = 1
        except Exception as e:
            print(f"=== cycle FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            ping_healthchecks(False)
            status = 1
        if args.once:
            return status
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
