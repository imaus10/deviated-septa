#!/usr/bin/env python
"""Rebuild local state from the S3 parquet eternal ledger (bootstrap + DR).

Streams each archive/observations/<sd>.parquet directly from S3 (via pyarrow,
one row group at a time — no local download/staging), keeps the 7-date store
window as raw rows, folds older dates into the all-time baseline, then runs the
same rollup tail as the live poller (daily chronicle + current.json + state) and
uploads the resulting artifacts.

Folded dates take their totals from the S3 daily chronicle (state/daily/<sd>.json)
whenever one exists — the same finalized totals the live poller folds at prune
time — and only fall back to re-deriving them from the raw parquet for dates that
predate the chronicle. That keeps a restore faithful to the live baseline even
where a raw archive is known-short (2026-09-08: parquet lost rows, the daily and
baseline retain the full totals); the restore warns when the two disagree.

Dates are enumerated from the union of all three sources — S3 archives, S3
dailies, and local dailies — so a date whose raw archive went missing is still
restored from whatever totals survived instead of being silently dropped. A full
restore rebuilds from scratch (so it can never double-count over an existing
state) but *stages* local dailies rather than deleting them, and if a date's only
remaining record is a local daily, it is folded and re-uploaded to S3.

Apply is the default; use --dry-run to preview without writing. Use --date to
restore a single service date.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

INGESTION_DIR = Path(__file__).resolve().parents[1]
if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

import poller.s3 as s3
from poller.archives import OBSERVATION_COLUMNS, read_archive_meta, stream_observation
from poller.constants import EASTERN
from poller.gtfs_static import load_local_metadata
from poller.rollup import (
    accumulate_totals,
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
DAILY_PREFIX = "state/daily/"
CURRENT_CACHE_CONTROL = "max-age=55, stale-while-revalidate=5"


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

def _archive_date_set() -> set[date]:
    """Dates that have a raw archive on S3, from the listing itself.

    The restore reuses this set rather than HEAD-ing each key, because
    `s3.object_exists` reports "absent" for *any* error — a transient HEAD
    failure would then read as a missing archive and silently drop a date that is
    really there. The listing we already did is exact and free.
    """
    keys = [k for k in s3.list_objects(OBSERVATION_PREFIX) if k.endswith(".parquet")]
    return {date.fromisoformat(k.split("/")[-1][:10]) for k in keys}


def _s3_daily_dates() -> list[date]:
    """Service dates that have a daily chronicle on S3 (no raw archive required)."""
    return sorted(
        date.fromisoformat(k.split("/")[-1][:10])
        for k in s3.list_objects(DAILY_PREFIX)
        if k.endswith(".json")
    )


def _local_daily_dates() -> list[date]:
    """Service dates with a daily chronicle on local disk."""
    daily_dir = STATE_DIR / "daily"
    if not daily_dir.is_dir():
        return []
    return sorted(
        date.fromisoformat(p.stem) for p in daily_dir.glob("*.json")
    )


def _restorable_dates(only: date | None) -> tuple[list[date], set[date]]:
    """Every service date with surviving records, plus the subset that has a raw
    archive on S3.

    The raw archive listing alone is not a safe universe of dates: an archive can
    go missing (external deletion, a lifecycle rule, a bad overwrite) while the
    daily chronicle — on S3 or locally — still holds the finalized totals.
    Enumerating only archives would make such a date invisible, and the full
    restore would then also stage away the local copy. Union all three sources so
    a date is restored from whatever survived, and never silently dropped.
    """
    archives = _archive_date_set()
    dates = archives | set(_s3_daily_dates()) | set(_local_daily_dates())
    if only:
        dates = {d for d in dates if d == only}
    return sorted(dates), dates & archives


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def _row_tuples(batch) -> list[tuple]:
    cols = [batch.column(c).to_pylist() for c in OBSERVATION_COLUMNS]
    return list(zip(*cols))


def read_s3_daily(sd: str) -> dict | None:
    """Load a service date's totals-only chronicle from S3, or None if absent."""
    return s3.read_json(f"{DAILY_PREFIX}{sd}.json")


def _fold_from_daily(baseline: dict, sd: str, key: str, fs) -> bool | None:
    """Fold a service date into the baseline from its S3 daily chronicle.

    The daily chronicle holds the finalized per-route/per-stop totals for a
    service date — the same numbers the live poller folds into the baseline when
    the date ages out — so prefer it over re-deriving totals from the raw parquet.
    This keeps a DR restore faithful to the live baseline even when an archive is
    known-short (see 2026-09-08, whose parquet lost rows but whose daily — and
    baseline — retain the full totals).

    Returns True if folded from the daily, None if no daily exists (caller folds
    from parquet instead). Warns when the daily total exceeds the archive's row
    count, which flags a known-short raw ledger without failing the restore.
    """
    daily = read_s3_daily(sd)
    if not daily:
        return None
    add_to_baseline(baseline, daily)
    daily_total = sum(t.get("total_observations", 0) for t in (daily.get("routes") or {}).values())
    try:
        _as_of, archive_rows = read_archive_meta(s3.full_path(key), filesystem=fs)
        if archive_rows is not None and archive_rows < daily_total:
            log(
                f"  {sd}: WARNING daily totals ({daily_total:,}) exceed raw archive "
                f"rows ({archive_rows:,}) — raw ledger is short for this date; "
                f"folding the daily totals"
            )
    except Exception as e:  # archive unreadable / missing — daily totals still authoritative
        log(f"  {sd}: could not read archive row count ({type(e).__name__}: {e})")
    log(f"  {sd}: folded totals from daily chronicle")
    return True


def _fold_from_local_daily(baseline: dict, sd: str, daily_dir: Path) -> bool:
    """Fold from a *local* daily — the last surviving copy of a date.

    Reached only when a service date has neither an S3 archive nor an S3 daily
    (both lost), so this file is the sole record of its finalized totals. Fold it
    into the baseline and best-effort re-upload it to S3 so the eternal ledger
    regains its copy — a local-only date would otherwise vanish from the ledger
    entirely the next time this machine is lost.

    `daily_dir` is the staged directory after a full restore moved state/daily/
    aside, or the live state/daily/ for a targeted --date restore.
    """
    path = daily_dir / f"{sd}.json"
    if not path.exists():
        return False
    daily = json.loads(path.read_text(encoding="utf-8"))
    if not (daily.get("routes") or daily.get("stops")):
        return False
    add_to_baseline(baseline, daily)
    total = sum(t.get("total_observations", 0) for t in (daily.get("routes") or {}).values())
    log(
        f"  {sd}: WARNING no S3 archive or daily chronicle for this date — "
        f"folded totals from the local daily ({total:,} observations), the last copy"
    )
    if s3.upload(f"{DAILY_PREFIX}{sd}.json", path):
        log(f"  {sd}: re-uploaded local daily chronicle to S3")
    else:
        log(f"  {sd}: WARNING could not re-upload local daily to S3")
    return True


def _stage_local_dailies() -> Path | None:
    """Move state/daily/ aside so a full restore rebuilds without double-counting,
    while keeping the files as a fallback + as the possible last copy of a date.

    Returns the stage dir, or None if there was nothing to stage. On success the
    caller removes it; on failure it is left in place and reported, so a restore
    that can't re-derive a date never destroys the local record of it.
    """
    daily_dir = STATE_DIR / "daily"
    if not daily_dir.is_dir():
        return None
    stage_dir = STATE_DIR / f"daily.stage-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    shutil.move(str(daily_dir), str(stage_dir))
    log(f"  staged {len(list(stage_dir.glob('*.json')))} local daily file(s) to "
        f"{stage_dir.name}/ (rebuild will regenerate state/daily/)")
    return stage_dir


def _reupload_local_daily(sd: str, daily_dir: Path) -> bool:
    """Push a local daily back to S3 when the ledger's copy of it is gone.

    Recovery only: it restores the eternal ledger without folding anything, so it
    can never perturb the baseline's ordering. Returns True if uploaded.
    """
    path = daily_dir / f"{sd}.json"
    if not path.exists():
        return False
    if s3.upload(f"{DAILY_PREFIX}{sd}.json", path):
        log(f"  {sd}: re-uploaded local daily chronicle to S3 (recovering the ledger)")
        return True
    log(f"  {sd}: WARNING could not re-upload local daily to S3")
    return False


def _store_window_date(db, sd: str, key: str, fs, local_daily_dir,
                      has_archive: bool) -> None:
    """Load a window date's raw rows into the store.

    A window date with no raw archive cannot be reconstructed, and its totals are
    deliberately NOT folded into the baseline: prune only folds dates newer than
    `baseline.max_service_date`, so advancing that marker past still-unfolded store
    dates would strand them in between — they'd age out and drain without ever
    being folded, losing their totals. The day is flagged instead, and its totals
    stay recoverable from the daily chronicle (re-uploaded from local if needed).
    """
    if has_archive:
        rows = 0
        for batch in stream_observation(s3.full_path(key), filesystem=fs):
            db.load_archive(_row_tuples(batch))
            rows += batch.num_rows
        log(f"  {sd}: stored {rows} rows")
        return
    if not read_s3_daily(sd):
        _reupload_local_daily(sd, local_daily_dir)
    log(f"  {sd}: WARNING no raw archive on S3 — store window has no rows for this "
        f"date; totals remain in the daily chronicle")


def _fold_date(baseline: dict, sd: str, key: str, fs, local_daily_dir,
               has_archive: bool) -> None:
    """Fold an out-of-window date into the baseline, preferring the most
    authoritative surviving source: S3 daily > S3 parquet > local daily."""
    if _fold_from_daily(baseline, sd, key, fs):
        return
    if has_archive:
        rows = 0
        routes, stops = {}, {}
        for batch in stream_observation(s3.full_path(key), filesystem=fs):
            accumulate_totals(routes, stops, batch)
            rows += batch.num_rows
        add_to_baseline(baseline, {"service_date": sd, "routes": routes, "stops": stops})
        log(f"  {sd}: folded {rows} rows from parquet (no daily chronicle)")
        return
    if local_daily_dir and _fold_from_local_daily(baseline, sd, local_daily_dir):
        return
    log(f"  {sd}: WARNING no surviving records (no archive, no S3/local daily) — not folded")


def restore(args: argparse.Namespace) -> None:
    dates, archive_dates = _restorable_dates(args.date)
    if not dates:
        log("No restorable service dates found (S3 archives, S3 dailies, local dailies).")
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

    # A full restore rebuilds the baseline from scratch, so any existing local
    # baseline must go first — folding the ledger on top of one would double-count
    # every date it already holds. Local dailies are staged (moved aside), not
    # deleted: the rebuild regenerates state/daily/ from the restored store, but
    # the staged files stay available as the last-resort source (and possible last
    # copy) for any date whose S3 archive is gone. (A --date restore is a targeted
    # single-date rebuild and leaves everything alone.)
    stage_dir = None
    if args.date is None:
        baseline_path = STATE_DIR / "all-baseline.json"
        if baseline_path.exists():
            baseline_path.unlink()
        log("  wiped stale all-baseline.json before rebuild")
        stage_dir = _stage_local_dailies()

    # Where to look for a last-resort local daily: the stage dir after a full
    # restore moved it aside, or the live state/daily/ for a --date restore.
    local_daily_dir = stage_dir or (STATE_DIR / "daily")

    db = ObservationsDB(db_path)
    baseline = load_baseline(STATE_DIR)
    try:
        for d in dates:
            sd = d.isoformat()
            key = f"{OBSERVATION_PREFIX}/{sd}.parquet"
            has_archive = d in archive_dates
            if d >= window_low:
                _store_window_date(db, sd, key, fs, local_daily_dir, has_archive)
            else:
                _fold_date(baseline, sd, key, fs, local_daily_dir, has_archive)
    except Exception:
        db.close()
        if stage_dir:
            log(f"\nRestore failed; staged local dailies preserved at {stage_dir} — "
                f"rerun to retry or inspect before deleting.")
        raise
    else:
        db.close()

    save_baseline(STATE_DIR, baseline)
    log(f"  baseline saved (min={baseline.get('min_service_date')}, "
        f"max={baseline.get('max_service_date')})")

    db = ObservationsDB(db_path)
    try:
        for sd in refresh_daily_chronicle(
            db, STATE_DIR, current_sd,
            folded_through=baseline.get("max_service_date"),
        ):
            s3.upload(f"{DAILY_PREFIX}{sd}.json", STATE_DIR / "daily" / f"{sd}.json")

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

    # Everything rebuilt cleanly — the staged files are redundant now, and any
    # last-copy daily has already been re-uploaded to S3.
    if stage_dir:
        shutil.rmtree(stage_dir, ignore_errors=True)
        log(f"  removed staged dailies ({stage_dir.name}/) after successful rebuild")

    log("Restore complete.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    restore(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
