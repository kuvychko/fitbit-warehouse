"""Timestamp and value helpers shared by the Takeout parsers."""

from __future__ import annotations

from datetime import date, datetime, timezone


def utc_ts(s: str) -> datetime:
    """ISO-8601 with Z or offset, e.g. '2024-05-01T00:00:03Z' (newer CSVs)."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def classic_utc_ts(s: str) -> datetime:
    """Classic-JSON intraday 'MM/DD/YY HH:MM:SS' — values are UTC despite the
    files being bounded by local day (see docs/takeout-format.md)."""
    return datetime.strptime(s, "%m/%d/%y %H:%M:%S").replace(tzinfo=timezone.utc)


def local_ts(s: str, tz) -> datetime:
    """Naive local timestamp (AZM, temperature, sleep/weight JSON) → UTC."""
    return datetime.fromisoformat(s).replace(tzinfo=tz).astimezone(timezone.utc)


def classic_date(s: str) -> date:
    """Classic-JSON daily 'MM/DD/YY[ HH:MM:SS]' → date."""
    return datetime.strptime(s.split(" ")[0], "%m/%d/%y").date()


def opt_float(s) -> float | None:
    """'' / None / sentinel -1.0 → None (Takeout uses -1.0 for 'no data')."""
    if s is None or s == "":
        return None
    v = float(s)
    return None if v == -1.0 else v


def opt_int(s) -> int | None:
    return None if s is None or s == "" else int(float(s))


LBS_PER_KG = 2.2046226218


def to_kg(value: float, unit: str) -> float:
    return value / LBS_PER_KG if unit == "lbs" else value
