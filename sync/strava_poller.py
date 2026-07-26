"""Strava API sync poller.

    python -m sync.strava_poller [--once]

Every STRAVA_POLL_INTERVAL seconds (default 21600 = 6 h) pulls new activities
from the Strava API and upserts into health.strava_activity /
health.activity_track.

Catch-up is DB-driven: the cursor is max(start_time) in strava_activity minus
STRAVA_OVERLAP_HOURS, so a late-uploaded activity near the previous cursor is
never skipped and missed cycles self-heal (natural-key upserts absorb overlap).

Track points are only fetched for activities not already present in
activity_track — a load-bearing guard (design Decision 10): it stops a normal
sync from re-deriving a backfilled run's track. Each point's absolute time is
start_date + offset, the same rule backfill uses, so keys match either way.

Rate limits (429) end the cycle gracefully; the next cycle covers the gap. A
Healthchecks ping fires only after a fully successful cycle.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backfill import db
from sync.strava_authorize import api_get, load_dotenv, refresh_access_token, token_path

SOURCE = "strava-api"


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


class CycleAbort(Exception):
    """Rate limit / auth trouble: end this cycle, next one catches up."""


def _check(status, what, body):
    if status == 429:
        raise CycleAbort(f"429 rate-limited on {what}")
    if status != 200:
        raise CycleAbort(f"HTTP {status} on {what}: {body[:300]}")


# --- mappers: API payload -> table rows (column order per backfill.db.TABLES) ---

def map_activity(a: dict):
    """Strava summary activity -> strava_activity row. Units already match the
    backfill: distance in meters, times in seconds. elev_loss_m is not in the
    summary payload (backfill supplies it; the COALESCE upsert keeps it)."""
    off = a.get("utc_offset")
    yield ("strava_activity", (
        int(a["id"]),
        ts(a["start_date"]),
        int(off) if off is not None else None,
        a.get("type"),                       # coarse type, matches CSV "Activity Type"
        a.get("name"),
        float(a["distance"]) if a.get("distance") is not None else None,
        int(a["moving_time"]) if a.get("moving_time") is not None else None,
        int(a["elapsed_time"]) if a.get("elapsed_time") is not None else None,
        float(a["total_elevation_gain"]) if a.get("total_elevation_gain") is not None else None,
        None,                                # elev_loss not in summary
        SOURCE))


def map_streams(activity_id: int, start: datetime, streams: dict):
    """/streams (key_by_type) -> activity_track rows. time = start + offset,
    identical to the backfill's derivation."""
    time_arr = (streams.get("time") or {}).get("data") or []
    latlng = (streams.get("latlng") or {}).get("data") or []
    alt = (streams.get("altitude") or {}).get("data") or []
    for i, off in enumerate(time_arr):
        ll = latlng[i] if i < len(latlng) else None
        lat, lon = (ll[0], ll[1]) if ll else (None, None)
        ele = alt[i] if i < len(alt) else None
        yield ("activity_track",
               (activity_id, start + timedelta(seconds=int(off)), lat, lon, ele, SOURCE))


# --- DB-driven cursor + track existence guard ---------------------------------

def cursor_after(conn, overlap_h: int) -> int:
    """Epoch seconds for the `after` filter: newest stored start minus overlap.
    0 (all history) when the table is empty — expected only before a backfill."""
    with conn.cursor() as cur:
        cur.execute("SELECT max(start_time) FROM health.strava_activity")
        (last,) = cur.fetchone()
    if last is None:
        return 0
    return int((last - timedelta(hours=overlap_h)).timestamp())


def has_track(conn, activity_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM health.activity_track WHERE activity_id = %s LIMIT 1",
                    (activity_id,))
        return cur.fetchone() is not None


def pull_activities(access, after: int, allow: set[str]):
    """Paginated /athlete/activities?after=..., filtered client-side to the
    coarse type allowlist (the list endpoint has no server-side type filter)."""
    page = 1
    while True:
        status, body = api_get(access, "/athlete/activities",
                               {"after": after, "page": page, "per_page": 200})
        _check(status, "athlete/activities", body)
        batch = json.loads(body)
        if not batch:
            return
        for a in batch:
            if not allow or a.get("type") in allow:
                yield a
        page += 1


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
    overlap_h = int(os.environ.get("STRAVA_OVERLAP_HOURS", "24"))
    allow = {t.strip() for t in os.environ.get("STRAVA_ACTIVITY_TYPES", "Run,Hike").split(",")
             if t.strip()}
    access = refresh_access_token(
        os.environ["STRAVA_CLIENT_ID"], os.environ["STRAVA_CLIENT_SECRET"], token_path())

    after = cursor_after(conn, overlap_h)
    loader = db.Loader(conn)
    activities = list(pull_activities(access, after, allow))
    print(f"  activities after {datetime.fromtimestamp(after, tz=timezone.utc):%Y-%m-%d} "
          f"-> {len(activities)} matching {sorted(allow)}")
    for a in activities:
        for trow in map_activity(a):
            loader.add(*trow)
    loader.flush()  # land summaries before pulling tracks

    new_tracks = pts = 0
    for a in activities:
        aid = int(a["id"])
        if has_track(conn, aid):
            continue
        status, body = api_get(access, f"/activities/{aid}/streams",
                               {"keys": "time,latlng,altitude", "key_by_type": "true"})
        if status == 404:
            continue  # manual/indoor activity with no GPS stream
        _check(status, f"streams/{aid}", body)
        n = 0
        for trow in map_streams(aid, ts(a["start_date"]), json.loads(body)):
            loader.add(*trow)
            n += 1
        new_tracks += 1
        pts += n
    loader.flush()
    print(f"  streams pulled for {new_tracks} new activities -> {pts} points")
    for table, n in sorted(loader.written.items()):
        print(f"  written: {n:>8}  {table}")


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="single cycle, then exit")
    args = ap.parse_args()
    interval = int(os.environ.get("STRAVA_POLL_INTERVAL", "21600"))

    missing = [v for v in ("HEALTH_RW_PW", "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET")
               if not os.environ.get(v)]
    if missing:
        print(f"ERROR: missing required env: {', '.join(missing)} (see .env.example)",
              file=sys.stderr)
        return 2
    if not token_path().is_file():
        print(f"ERROR: no token at {token_path()} — run `python -m sync.strava_authorize` once",
              file=sys.stderr)
        return 2

    while True:
        started = datetime.now(timezone.utc)
        print(f"=== strava cycle start {started:%Y-%m-%d %H:%M:%S}Z")
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
