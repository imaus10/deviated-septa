#!/usr/bin/env python
"""Rebuild local state from the S3 parquet eternal ledger (bootstrap + DR).

Streams each archive/observations/<sd>.parquet directly from S3 (via pyarrow,
one row group at a time — no local download/staging), keeps the 7-date store
window as raw rows, folds older dates into the all-time baseline, then runs the
same rollup tail as the live poller (daily chronicle + current.json + state) and
uploads the resulting artifacts.

Apply is the default; use --dry-run to preview without writing. Use --date to
restore a single service date.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

INGESTION_DIR = Path(__file__).resolve().parents[1]
if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

import poller.s3 as s3
from poller.archives import OBSERVATION_COLUMNS, stream_observation
from poller.constants import EASTERN
from poller.gtfs_static import load_local_metadata
from poller.rollup import (
    add_to_baseline,
    build_current,
    load_baseline,
    refresh_daily_chronicle,
    save_baseline,
    write_json,
)
from poller.state import ObservationsDB, save_state

load_dotenv(INGESTION_DIR.parent / ".env")

ROOT = INGESTION_DIR
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / "state"

OBSERVATION_PREFIX = "archive/observations"
CURRENT_CACHE_CONTROL = "max-age=55, stale-while-revalidate=5"
TOTAL_KEYS = ("on_time_count", "early_count", "late_count")


def log(message: str = "") -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a valid YYYY-MM-DD date"
        ) from exc


def daterange_delta(d: date, days: int) -> date:
    from datetime import timedelta

    return d - timedelta(days=days)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild local state from the S3 parquet ledger."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview what would be restored without writing anything",
    )
    parser.add_argument(
        "--date",
        type=parse_date,
        help="restore only this single YYYY-MM-DD service date",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Archive fetch
# ---------------------------------------------------------------------------

def _archive_dates(only: date | None) -> list[date]:
    keys = [k for k in s3.list_objects(OBSERVATION_PREFIX) if k.endswith(".parquet")]
    dates = sorted(
        date.fromisoformat(k.split("/")[-1][:10]) for k in keys
    )
    if only:
        dates = [d for d in dates if d == only]
    return dates


# ---------------------------------------------------------------------------
# Fold — accumulate per-key totals from RecordBatches (rollup shape)
# ---------------------------------------------------------------------------

def _add_total(out: dict, key, category, delay) -> None:
    t = out.get(key)
    if t is None:
        t = out[key] = {
            "total_observations": 0,
            **{k: 0 for k in TOTAL_KEYS},
            "delay_sum": 0,
        }
    t["total_observations"] += 1
    t[f"{category}_count"] += 1
    t["delay_sum"] += delay or 0


def _accumulate_totals(routes: dict, stops: dict, batch) -> None:
    """Fold one RecordBatch's per-route/per-stop totals into the accumulators."""
    rid_col = batch.column("route_id").to_pylist()
    sid_col = batch.column("stop_id").to_pylist()
    cat_col = batch.column("category").to_pylist()
    delay_col = batch.column("delay_seconds").to_pylist()
    for rid, sid, cat, delay in zip(rid_col, sid_col, cat_col, delay_col):
        _add_total(routes, rid, cat, delay)
        _add_total(stops, sid, cat, delay)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def _row_tuples(batch) -> list[tuple]:
    cols = [batch.column(c).to_pylist() for c in OBSERVATION_COLUMNS]
    return list(zip(*cols))


def restore(args: argparse.Namespace) -> None:
    dates = _archive_dates(args.date)
    if not dates:
        log("No archive dates found on S3.")
        return

    mode = "dry-run" if args.dry_run else "APPLY"
    log(f"[{mode}] {len(dates)} service date(s) to restore")

    if args.dry_run:
        for d in dates:
            log(f"  {d.isoformat()}")
        log("\nDry run only — nothing was written.")
        return

    current_sd = dates[-1].isoformat()
    window_low = daterange_delta(dates[-1], 6)
    log(f"  current service date: {current_sd} (window low: {window_low.isoformat()})")

    fs = s3.filesystem()

    db_path = STATE_DIR / "observations.db"
    for suffix in ("", "-wal", "-shm"):
        fp = Path(f"{db_path}{suffix}")
        if fp.exists():
            fp.unlink()

    db = ObservationsDB(db_path)
    baseline = load_baseline(STATE_DIR)
    try:
        for d in dates:
            sd = d.isoformat()
            key = f"{OBSERVATION_PREFIX}/{sd}.parquet"
            rows = 0
            if d >= window_low:
                for batch in stream_observation(s3.full_path(key), filesystem=fs):
                    db.load_archive(_row_tuples(batch))
                    rows += batch.num_rows
                log(f"  {sd}: stored {rows} rows")
            else:
                routes, stops = {}, {}
                for batch in stream_observation(s3.full_path(key), filesystem=fs):
                    _accumulate_totals(routes, stops, batch)
                    rows += batch.num_rows
                add_to_baseline(baseline, {"service_date": sd, "routes": routes, "stops": stops})
                log(f"  {sd}: folded {rows} rows into baseline")
    finally:
        db.close()

    save_baseline(STATE_DIR, baseline)
    log(f"  baseline saved (min={baseline.get('min_service_date')}, "
        f"max={baseline.get('max_service_date')})")

    db = ObservationsDB(db_path)
    try:
        for sd in refresh_daily_chronicle(db, STATE_DIR, current_sd):
            s3.upload(f"state/daily/{sd}.json", STATE_DIR / "daily" / f"{sd}.json")

        data = load_local_metadata(str(DATA_DIR))
        current = build_current(db, data, STATE_DIR, current_sd=current_sd)
        write_json(current, STATE_DIR / "current.json")
        s3.upload("public/current.json", STATE_DIR / "current.json",
                cache_control=CURRENT_CACHE_CONTROL)
        log(f"  current.json written ({current['current_service_date']})")

        save_state(STATE_DIR, current_sd, datetime.now(timezone.utc).timestamp())

        baseline_path = STATE_DIR / "all-baseline.json"
        s3.upload("state/all-baseline.json", baseline_path)
    finally:
        db.close()

    log("Restore complete.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    restore(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
