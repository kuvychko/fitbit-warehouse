"""Database access: table registry and staged batch upserts.

Rows are COPYed into a temp staging table, then upserted with
INSERT ... SELECT DISTINCT ON (key) ... ON CONFLICT — so re-runs and
overlapping files are absorbed by the natural keys (see the health-schema
spec) and duplicates inside a single batch cannot break DO UPDATE.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class TableSpec:
    name: str                # table in the health schema
    cols: tuple[str, ...]    # insert column order; parsers yield tuples in this order
    key: tuple[str, ...]     # natural-key (conflict) columns
    action: str              # 'nothing' (immutable samples) | 'update' (revisable summaries)


TABLES: dict[str, TableSpec] = {
    t.name: t
    for t in [
        TableSpec("heart_rate", ("time", "bpm", "confidence", "source", "device"), ("time",), "nothing"),
        TableSpec("steps", ("time", "steps", "source", "device"), ("time",), "nothing"),
        TableSpec("calories", ("time", "kcal", "source", "device"), ("time",), "nothing"),
        TableSpec("distance", ("time", "meters", "source", "device"), ("time",), "nothing"),
        TableSpec("floors", ("time", "floors", "source", "device"), ("time",), "nothing"),
        TableSpec("spo2", ("time", "pct", "source", "device"), ("time",), "nothing"),
        TableSpec("hrv", ("time", "rmssd_ms", "sdrr_ms", "source", "device"), ("time",), "nothing"),
        TableSpec("device_temperature", ("time", "deviation_c", "sensor_type", "source", "device"), ("time",), "nothing"),
        TableSpec("azm", ("time", "zone", "minutes", "source", "device"), ("time", "zone"), "nothing"),
        TableSpec("sleep_stage", ("time", "level", "seconds", "is_short", "source"), ("time", "level"), "nothing"),
        TableSpec(
            "sleep_session",
            ("start_time", "end_time", "day", "log_id", "duration_ms", "minutes_asleep",
             "minutes_awake", "minutes_to_fall_asleep", "minutes_after_wakeup", "time_in_bed",
             "efficiency", "sleep_type", "log_type", "main_sleep", "levels_summary",
             "source", "device"),
            ("start_time",), "update",
        ),
        TableSpec(
            "sleep_score",
            ("time", "sleep_log_id", "overall", "composition", "revitalization",
             "duration_score", "deep_sleep_min", "resting_hr", "restlessness", "source"),
            ("time",), "update",
        ),
        TableSpec("breathing_rate", ("sleep_end", "deep_bpm", "light_bpm", "rem_bpm", "full_bpm", "source", "device"), ("sleep_end",), "update"),
        TableSpec("nightly_temperature", ("sleep_start", "sleep_end", "avg_c", "samples", "stdev_c", "source", "device"), ("sleep_start",), "update"),
        TableSpec("resting_heart_rate", ("day", "bpm", "error_bpm", "source"), ("day",), "update"),
        TableSpec("active_minutes_daily", ("day", "sedentary_min", "lightly_min", "moderately_min", "very_min", "source"), ("day",), "update"),
        TableSpec("hrv_daily", ("day", "rmssd_ms", "nremhr_bpm", "entropy", "source"), ("day",), "update"),
        TableSpec("weight", ("time", "weight_kg", "bmi", "source", "device"), ("time",), "update"),
        TableSpec("body_fat", ("time", "fat_pct", "source", "device"), ("time",), "update"),
    ]
}

# Column used for the MIN/MAX verification report (== first key column).
def time_col(spec: TableSpec) -> str:
    return spec.key[0]


def connect() -> psycopg.Connection:
    """Connect as health_rw using the same .env variables as the rest of the
    project. search_path comes from the role, set by migration 001."""
    return psycopg.connect(
        host=os.environ.get("PG_HOST", "localhost"),
        port=int(os.environ.get("PG_PORT", "5432")),
        dbname=os.environ.get("PG_DB", "warehouse"),
        user=os.environ.get("PG_USER", "health_rw"),
        password=os.environ["HEALTH_RW_PW"],
    )


class Loader:
    """Batches rows per table and upserts via a temp staging table."""

    BATCH = 50_000

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn
        self._pending: dict[str, list[tuple]] = {}
        self._staged: set[str] = set()
        self.written: dict[str, int] = {}  # rows affected per table

    def add(self, table: str, row: tuple) -> None:
        buf = self._pending.setdefault(table, [])
        buf.append(row)
        if len(buf) >= self.BATCH:
            self._flush(table)

    def flush(self) -> None:
        for table in list(self._pending):
            self._flush(table)

    def _flush(self, table: str) -> None:
        rows = self._pending.get(table)
        if not rows:
            return
        spec = TABLES[table]
        stage = f"stage_{table}"
        cols = ", ".join(spec.cols)
        key = ", ".join(spec.key)
        with self.conn.cursor() as cur:
            if table not in self._staged:
                cur.execute(
                    f"CREATE TEMP TABLE {stage} (LIKE health.{table}) ON COMMIT PRESERVE ROWS"
                )
                self._staged.add(table)
            with cur.copy(f"COPY {stage} ({cols}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
            if spec.action == "update":
                # COALESCE: a revision wins where it has a value, but never
                # nulls out an enrichment another source provided (e.g. the
                # API poller re-pulling a sleep session the Takeout backfill
                # loaded with efficiency/levels the API does not carry).
                sets = ", ".join(
                    f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})"
                    for c in spec.cols if c not in spec.key
                )
                conflict = f"DO UPDATE SET {sets}"
            else:
                conflict = "DO NOTHING"
            cur.execute(
                f"INSERT INTO health.{table} ({cols}) "
                f"SELECT DISTINCT ON ({key}) {cols} FROM {stage} ORDER BY {key} "
                f"ON CONFLICT ({key}) {conflict}"
            )
            self.written[table] = self.written.get(table, 0) + cur.rowcount
            cur.execute(f"TRUNCATE {stage}")
        self.conn.commit()
        self._pending[table] = []

    def table_stats(self) -> list[tuple[str, int, str, str]]:
        """(table, row count, min time, max time) for the verification report."""
        out = []
        with self.conn.cursor() as cur:
            for spec in TABLES.values():
                tc = time_col(spec)
                cur.execute(
                    f"SELECT count(*), min({tc}), max({tc}) FROM health.{spec.name}"
                )
                n, lo, hi = cur.fetchone()
                out.append((spec.name, n, str(lo), str(hi)))
        return out
