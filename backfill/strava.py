"""Parser for a bulk Strava account export.

Format quirks are documented in docs/strava-format.md. Unlike the Fitbit
export (per-file streams), Strava is one `activities.csv` driving everything
plus a sibling `activities/*.gpx` directory, so a single stream entry matches
`activities.csv` and its parser reads the GPX tracks alongside it.

Two rules from the design:
  * CSV is the authoritative summary; GPX is track-only, loaded verbatim
    (no recomputation of distance/elevation/pace from the points).
  * activity_track.time is derived as start_time + round(offset) — identical
    to how the API sync derives it — so a GPX-loaded track and an API-synced
    track for the same activity share keys and overlap dedups.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .util import opt_float, opt_int, utc_ts

SOURCE = "strava-export"

# Strava's activities.csv "Activity Date", e.g. "Jul 15, 2026, 6:08:13 PM".
# It is UTC (verified against each GPX's <metadata><time>).
_CSV_DATE_FMT = "%b %d, %Y, %I:%M:%S %p"


def _parse_start(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), _CSV_DATE_FMT).replace(tzinfo=timezone.utc)


def _col_map(header: list[str]) -> dict[str, list[int]]:
    """Header name -> list of column indices. The export repeats several names
    (`Distance`, `Elapsed Time`): the SECOND `Distance` is meters (the first is
    the account's display unit, km or mi — not portable), so callers pick by
    occurrence."""
    idx: dict[str, list[int]] = {}
    for i, name in enumerate(header):
        idx.setdefault(name, []).append(i)
    return idx


def _cell(row: list[str], indices: list[int] | None, which: int = 0) -> str | None:
    if not indices:
        return None
    i = indices[which]
    return row[i] if i < len(row) else None


def _gpx_path(root: Path, filename: str | None, activity_id: int) -> Path | None:
    """Resolve an activity's track file. Prefer the CSV `Filename` column;
    fall back to activities/<id>.gpx. Supports plain and gzipped GPX."""
    candidates = []
    if filename:
        candidates.append(root / filename)
    candidates += [root / "activities" / f"{activity_id}.gpx",
                   root / "activities" / f"{activity_id}.gpx.gz"]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _gpx_points(path: Path) -> Iterator[tuple[datetime, float, float, float | None]]:
    """Yield (abs_time_utc, lat, lon, elevation_m) per <trkpt>. GPX <time> is
    absolute UTC; no <extensions> (HR/cadence/power) exist in the bulk export."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:
        for _event, elem in ET.iterparse(fh):
            tag = elem.tag.rsplit("}", 1)[-1]  # strip namespace
            if tag != "trkpt":
                continue
            lat = elem.get("lat")
            lon = elem.get("lon")
            ele = t = None
            for child in elem:
                ctag = child.tag.rsplit("}", 1)[-1]
                if ctag == "ele":
                    ele = child.text
                elif ctag == "time":
                    t = child.text
            elem.clear()  # keep memory flat across thousands of points
            if lat is None or lon is None or not t:
                continue
            yield (utc_ts(t), float(lat), float(lon),
                   float(ele) if ele not in (None, "") else None)


def _track_rows(activity_id: int, start: datetime, gpx: Path
                ) -> Iterator[tuple[str, tuple]]:
    """activity_track rows with time canonically derived as start + round(offset)
    (design Decision 10), so backfill and API sync produce identical keys."""
    for abs_time, lat, lon, ele in _gpx_points(gpx):
        offset = round((abs_time - start).total_seconds())
        yield ("activity_track",
               (activity_id, start + timedelta(seconds=offset), lat, lon, ele, SOURCE))


def parse_activities(path: Path, ctx) -> Iterator[tuple[str, tuple]]:
    """Parse activities.csv, filter to ctx.strava_types (coarse Activity Type),
    emit strava_activity + activity_track rows. Reports type-skips, activities
    missing a GPX, and orphan GPX files (no CSV row) loudly at the end."""
    root = path.parent
    allow = ctx.strava_types
    skipped_type: Counter = Counter()
    missing_gpx: list[int] = []
    csv_ids: set[int] = set()
    referenced_gpx: set[str] = set()

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        cols = _col_map(header)
        for row in reader:
            aid_raw = _cell(row, cols.get("Activity ID"))
            if not aid_raw:
                continue
            activity_id = int(aid_raw)
            csv_ids.add(activity_id)
            atype = (_cell(row, cols.get("Activity Type")) or "").strip()
            if allow and atype not in allow:
                skipped_type[atype or "(blank)"] += 1
                continue

            start = _parse_start(_cell(row, cols.get("Activity Date")))
            name = (_cell(row, cols.get("Activity Name")) or "").strip() or None
            # Distance: SECOND occurrence is meters (first is the display unit).
            distance_m = opt_float(_cell(row, cols.get("Distance"), which=-1))
            moving_s = opt_int(_cell(row, cols.get("Moving Time")))
            # Elapsed Time: use the detailed (second) occurrence for parity with
            # the meters block; both occurrences carry the same value.
            elapsed_s = opt_int(_cell(row, cols.get("Elapsed Time"), which=-1))
            elev_gain = opt_float(_cell(row, cols.get("Elevation Gain")))
            elev_loss = opt_float(_cell(row, cols.get("Elevation Loss")))
            yield ("strava_activity",
                   (activity_id, start, None, atype, name, distance_m,
                    moving_s, elapsed_s, elev_gain, elev_loss, SOURCE))

            filename = _cell(row, cols.get("Filename"))
            gpx = _gpx_path(root, filename, activity_id)
            if gpx is None:
                missing_gpx.append(activity_id)
                continue
            referenced_gpx.add(gpx.name)
            yield from _track_rows(activity_id, start, gpx)

    # --- loud reporting (strava-backfill spec) --------------------------------
    orphans = []
    act_dir = root / "activities"
    if act_dir.is_dir():
        for p in sorted(act_dir.glob("*.gpx*")):
            stem = p.name.split(".")[0]
            if stem.isdigit() and int(stem) not in csv_ids:
                orphans.append(p.name)
    if skipped_type:
        total = sum(skipped_type.values())
        detail = ", ".join(f"{v}×{k}" for k, v in skipped_type.most_common())
        print(f"  strava: skipped {total} activities by type (not in "
              f"{sorted(allow)}): {detail}")
    if missing_gpx:
        print(f"  strava: WARNING {len(missing_gpx)} allowlisted activities have "
              f"no GPX track (summary loaded, no points): "
              f"{missing_gpx[:10]}{' ...' if len(missing_gpx) > 10 else ''}")
    if orphans:
        print(f"  strava: WARNING {len(orphans)} GPX files have no activities.csv "
              f"row (not loaded): {orphans[:10]}{' ...' if len(orphans) > 10 else ''}")


# --- Stream registry (consumed by backfill.__main__) --------------------------

# One stream: activities.csv drives both tables; its parser reads the GPX dir.
STREAMS: list[tuple[str, re.Pattern, object]] = [
    ("strava", re.compile(r"(^|/)activities\.csv$"), parse_activities),
]

# The GPX tracks are loaded via activities.csv, and the rest of the export is
# social-graph / media data explicitly out of scope — recognize both so
# classify() doesn't flag them as unknown.
SKIP_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(^|/)activities/.*\.gpx(\.gz)?$", re.IGNORECASE), "GPS track (loaded via activities.csv)"),
    (re.compile(r"(^|/)activities/.*\.(fit|tcx)(\.gz)?$", re.IGNORECASE), "original activity file (not imported; GPX is the ceiling)"),
    (re.compile(r"\.(csv|json)$", re.IGNORECASE), "social-graph/account export (clubs, followers, gear, etc.) — out of scope"),
    (re.compile(r"\.(gpx)(\.gz)?$", re.IGNORECASE), "GPX outside activities/ (not loaded)"),
    (re.compile(r"\.(png|jpg|jpeg|gif|pdf|mp4|mov|txt|md|html)$", re.IGNORECASE), "media/readme file"),
]
