"""Parser for the optional Google Fit Takeout export (legacy Basis Peak data).

Google Fit "Data Points" JSON: epoch-nanosecond timestamps, values in
fitValue[].value.fpVal|intVal. Only raw Basis Peak device streams are loaded;
derived/merged streams and phone-sensor streams are duplicates or noise.
See docs/takeout-format.md.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

SOURCE = "googlefit-takeout"
DEVICE = "Basis Peak"

BASIS = r"Fit/All Data/raw_com\.google\.%s_com\.mybasis\.android\.basis\.peak_.*\.json$"

def _ts(nanos) -> datetime:
    return datetime.fromtimestamp(int(nanos) / 1e9, tz=timezone.utc)


def _iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_sleep_session(path: Path, ctx) -> Iterator[tuple[str, tuple]]:
    """Fit/All Sessions/*_SLEEP.json — Basis sleep sessions (UTC times,
    'sleep' segments but no stage vocabulary; gaps between segments = awake)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    start, end = _iso(doc["startTime"]), _iso(doc["endTime"])
    duration_s = int(doc.get("duration", "0s").rstrip("s") or 0)
    asleep_s = sum(
        (_iso(seg["endTime"]) - _iso(seg["startTime"])).total_seconds()
        for seg in doc.get("segment", [])
    )
    yield ("sleep_session", (
        start, end, start.astimezone(ctx.tz).date(),
        None, duration_s * 1000, round(asleep_s / 60),
        round((duration_s - asleep_s) / 60), None, None, round(duration_s / 60),
        None, None, "auto_detected", None, None, SOURCE, DEVICE))


# (name, filename regex, target table, value key, cast) — or (name, rx, parser)
STREAMS: list[tuple, ...] = [
    ("basis/heart_rate", re.compile(BASIS % r"heart_rate\.bpm"), "heart_rate", "fpVal", float),
    ("basis/calories", re.compile(BASIS % r"calories\.expended"), "calories", "fpVal", float),
    ("basis/steps", re.compile(BASIS % r"step_count\.delta"), "steps", "intVal", int),
    ("basis/sleep", re.compile(r"Fit/All Sessions/.*_SLEEP\.json$"), parse_sleep_session),
]

SKIP_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(BASIS % r"activity\.segment"), "Basis activity segments not imported in v1"),
    (re.compile(r"Fit/All Data/derived_"), "derived/merged Google Fit stream (duplicates raw data)"),
    (re.compile(r"Fit/All Data/raw_"), "phone/other-sensor stream, not a Basis Peak device stream"),
    (re.compile(r"Fit/All Sessions/"), "walking/running sessions not imported in v1"),
    (re.compile(r"Fit/Activities/"), "TCX activity files not imported"),
    (re.compile(r"Fit/Daily activity metrics/"), "phone-derived daily metrics not imported"),
]


def make_parser(table: str, value_key: str, cast):
    def parse(path: Path, ctx) -> Iterator[tuple[str, tuple]]:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for p in doc.get("Data Points", []):
            fit_value = p.get("fitValue") or []
            if not fit_value:
                continue
            v = (fit_value[0].get("value") or {}).get(value_key)
            if v is None:
                continue
            time = _ts(p["startTimeNanos"])
            if table == "heart_rate":
                row = (time, cast(v), None, SOURCE, DEVICE)
            else:
                row = (time, cast(v), SOURCE, DEVICE)
            if table == "steps":
                # Basis era predates the Health API: derive Fitbit-style
                # daily totals per local day (single device, no dedup hazard)
                day = time.astimezone(ctx.tz).date()
                ctx.basis_daily_steps[day] = ctx.basis_daily_steps.get(day, 0) + int(v)
            yield (table, row)

    return parse


def finish_steps_daily(ctx) -> Iterator[tuple[str, tuple]]:
    for day, total in sorted(ctx.basis_daily_steps.items()):
        yield ("steps_daily", (day, total, SOURCE, DEVICE))
