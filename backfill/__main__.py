"""Takeout backfill CLI.

    python -m backfill [--takeout-dir DIR] [--googlefit-dir DIR]
                       [--tz ZONE] [--only stream1,stream2] [--dry-run]

Defaults come from .env-style environment variables (TAKEOUT_DIR,
GOOGLEFIT_DIR, TAKEOUT_TZ, TAKEOUT_WEIGHT_UNIT, PG_*, HEALTH_RW_PW).
Idempotent: natural-key upserts absorb re-runs and API-sync overlap.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db, fitbit, googlefit


class Ctx:
    """Shared parser context (timezone, units, cross-file accumulators)."""

    def __init__(self, tz: str, weight_unit: str):
        self.tz = ZoneInfo(tz)
        self.weight_unit = weight_unit
        self.active_minutes: dict = {}
        self.basis_daily_steps: dict = {}


def classify(root: Path, streams, skip_patterns):
    """Walk an export dir; group files per stream, collect skips/unknowns."""
    matched: dict[str, list[Path]] = {}
    skipped: Counter = Counter()
    unknown: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root).as_posix()
        for name, rx, *_ in streams:
            if rx.search(rel):
                matched.setdefault(name, []).append(path)
                break
        else:
            for rx, reason in skip_patterns:
                if rx.search(rel):
                    skipped[reason] += 1
                    break
            else:
                unknown.append(rel)
    return matched, skipped, unknown


def run_streams(streams, matched, ctx, loader, errors):
    for entry in streams:
        name, parser = entry[0], entry[-1] if len(entry) == 3 else None
        files = matched.get(name)
        if not files:
            continue
        if parser is None:  # googlefit stream tuple: (name, rx, table, key, cast)
            _, _, table, value_key, cast = entry
            parser = googlefit.make_parser(table, value_key, cast)
        rows = 0
        for path in files:
            try:
                for table, row in parser(path, ctx):
                    loader.add(table, row)
                    rows += 1
            except Exception as e:  # keep loading other files, but report loudly
                errors.append(f"{path}: {type(e).__name__}: {e}")
        print(f"  {name}: {len(files)} file(s), {rows} rows parsed")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env reader (stdlib-only); real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="backfill", description=__doc__)
    ap.add_argument("--takeout-dir", default=os.environ.get("TAKEOUT_DIR"))
    ap.add_argument("--googlefit-dir", default=os.environ.get("GOOGLEFIT_DIR"))
    # No default on purpose: several streams carry naive local timestamps, and
    # silently assuming UTC corrupts them by the UTC offset (learned the hard
    # way). The loader refuses to guess.
    ap.add_argument("--tz", default=os.environ.get("TAKEOUT_TZ"))
    ap.add_argument("--weight-unit", default=os.environ.get("TAKEOUT_WEIGHT_UNIT", "lbs"),
                    choices=("lbs", "kg"))
    ap.add_argument("--only", help="comma-separated stream names (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="parse and report, no DB writes")
    ap.add_argument("--verbose", action="store_true", help="list every skipped file")
    args = ap.parse_args()

    if not args.takeout_dir and not args.googlefit_dir:
        ap.error("set --takeout-dir/TAKEOUT_DIR (and/or --googlefit-dir/GOOGLEFIT_DIR)")
    if not args.tz:
        ap.error("set --tz or TAKEOUT_TZ (IANA zone your Fitbit profile used, "
                 "e.g. America/Los_Angeles) — required to convert the export's "
                 "naive local timestamps; see docs/takeout-format.md")

    ctx = Ctx(args.tz, args.weight_unit)
    only = set(args.only.split(",")) if args.only else None
    errors: list[str] = []

    if args.dry_run:
        conn, loader = None, _DryLoader()
    else:
        conn = db.connect()
        loader = db.Loader(conn)

    exports = []
    if args.takeout_dir:
        exports.append(("Fitbit export", Path(args.takeout_dir),
                        fitbit.STREAMS, fitbit.SKIP_PATTERNS))
    if args.googlefit_dir:
        exports.append(("Google Fit export", Path(args.googlefit_dir),
                        googlefit.STREAMS, googlefit.SKIP_PATTERNS))

    all_skipped, all_unknown = Counter(), []
    for label, root, streams, skip_patterns in exports:
        if not root.is_dir():
            print(f"ERROR: {label}: {root} is not a directory", file=sys.stderr)
            return 2
        print(f"{label}: scanning {root}")
        matched, skipped, unknown = classify(root, streams, skip_patterns)
        if only:
            matched = {k: v for k, v in matched.items() if k in only}
        run_streams(streams, matched, ctx, loader, errors)
        all_skipped += skipped
        all_unknown += unknown

    # active-minutes rows are merged across four JSON families; emit them last
    am_rows = 0
    for table, row in fitbit.finish_active_minutes(ctx):
        loader.add(table, row)
        am_rows += 1
    if am_rows:
        print(f"  active_minutes_daily: {am_rows} merged day rows "
              "(the four active_minutes streams above accumulate here)")
    sd_rows = 0
    for table, row in googlefit.finish_steps_daily(ctx):
        loader.add(table, row)
        sd_rows += 1
    if sd_rows:
        print(f"  steps_daily: {sd_rows} Basis-era day totals")
    loader.flush()

    print("\n=== Skipped (recognized, intentionally not imported) ===")
    for reason, n in sorted(all_skipped.items()):
        print(f"  {n:6d}  {reason}")
    if all_unknown:
        print(f"\n=== UNKNOWN files (not recognized — investigate!) [{len(all_unknown)}] ===")
        for rel in all_unknown[: None if args.verbose else 40]:
            print(f"  {rel}")
        if not args.verbose and len(all_unknown) > 40:
            print(f"  ... and {len(all_unknown) - 40} more (use --verbose)")
    if errors:
        print(f"\n=== File errors [{len(errors)}] ===")
        for e in errors:
            print(f"  {e}")

    if conn is not None:
        print("\n=== Rows written this run (insert/update affected) ===")
        for table, n in sorted(loader.written.items()):
            print(f"  {n:10d}  {table}")
        print("\n=== Table totals (verify against raw files) ===")
        print(f"  {'table':<22} {'rows':>10}  time range")
        for table, n, lo, hi in loader.table_stats():
            # ASCII only: Windows consoles/redirects often default to cp1252
            print(f"  {table:<22} {n:>10}  {lo} .. {hi}")
        conn.close()
    else:
        print("\n(dry run: nothing written)")
        for table, n in sorted(loader.counts.items()):
            print(f"  {n:10d}  {table} rows parsed")

    return 1 if errors else 0


class _DryLoader:
    def __init__(self):
        self.counts: Counter = Counter()
        self.written: dict = {}

    def add(self, table, row):
        self.counts[table] += 1

    def flush(self):
        pass


if __name__ == "__main__":
    sys.exit(main())
