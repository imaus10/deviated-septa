#!/usr/bin/env python
"""Regenerate a missing daily chronicle entry (state/daily/<sd>.json) from its
raw archive, and publish it to the eternal ledger.

The daily chronicle is a totals-only snapshot the poller rewrites whenever the
store advances for a service date, and uploads best-effort. Because the upload
never crashes the poll and the local copy is deleted once the date drains, a
single transient upload failure at finalization time loses that date's chronicle
entry permanently (2026-09-09: the archive and baseline are complete, but the
daily never reached S3). This script rebuilds the missing entry by streaming the
raw archive, so the ledger gets its copy back without perturbing the baseline —
the date is already folded, and the totals are derived from the same rows the
fold used.

Apply is the default; use --dry-run to preview without writing anything.

Fail-closed guards (this never repairs a lossy archive into a "good" daily):
  - refuses to run if the S3 daily already exists (never overwrites a good copy);
  - refuses if the archive footer is unreadable (can't verify what we're reading);
  - refuses if the streamed rows disagree with the footer, or the totals don't
    reconcile to the row count;
  - refuses if the archive looks short next to its neighbours (e.g. the known
    2026-09-08 gap) unless --min-rows says otherwise.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

INGESTION_DIR = Path(__file__).resolve().parents[1]
if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

import poller.s3 as s3
from poller.archives import read_archive_meta, stream_observation
from poller.constants import CATEGORY_COUNT_KEYS, EASTERN
from poller.rollup import accumulate_totals, write_json

load_dotenv(INGESTION_DIR.parent / ".env")

STATE_DIR = INGESTION_DIR / "state"
OBSERVATION_PREFIX = "archive/observations"
DAILY_PREFIX = "state/daily/"
# A date whose archive has fewer rows than this fraction of its neighbours is
# treated as a known-short raw ledger (2026-09-08 holds 36K of a 770K day) and
# refused: a daily built from it would bake the loss into the chronicle.
SHORT_ARCHIVE_RATIO = 0.5


def log(message: str = "") -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill a missing daily chronicle entry from its raw archive."
    )
    parser.add_argument(
        "--date",
        required=True,
        help="service date to rebuild (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview the backfill without writing anything",
    )
    parser.add_argument(
        "--min-rows",
        type=int,
        default=None,
        help="override the short-archive row floor for this date",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Totals helpers
# ---------------------------------------------------------------------------

def _observations(totals: dict) -> int:
    return sum(t.get("total_observations", 0) for t in totals.values())


def _categories(totals: dict) -> int:
    return sum(
        t.get(k, 0) for t in totals.values() for k in CATEGORY_COUNT_KEYS
    )


# ---------------------------------------------------------------------------
# Archive context
# ---------------------------------------------------------------------------

def _archive_rows(sd: str, fs) -> int | None:
    """Row count from an archive footer, or None if it can't be read."""
    try:
        _as_of, rows = read_archive_meta(
            s3.full_path(f"{OBSERVATION_PREFIX}/{sd}.parquet"), filesystem=fs
        )
    except Exception:
        return None
    return rows


def _neighbour_rows(sd: str, fs) -> dict[str, int]:
    """Row counts for the nearest archived dates on each side (best effort)."""
    try:
        keys = [k for k in s3.list_objects(OBSERVATION_PREFIX) if k.endswith(".parquet")]
        dates = sorted(date.fromisoformat(k.split("/")[-1][:10]) for k in keys)
    except Exception:
        return {}
    target = date.fromisoformat(sd)
    out: dict[str, int] = {}
    for candidate in (max((d for d in dates if d < target), default=None),
                      min((d for d in dates if d > target), default=None)):
        if candidate is None:
            continue
        rows = _archive_rows(candidate.isoformat(), fs)
        if rows is not None:
            out[candidate.isoformat()] = rows
    return out


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_daily(sd: str, fs) -> tuple[dict, int]:
    """Stream a raw archive into a daily chronicle payload.

    Returns (daily, rows). Raises SystemExit when the archive is unusable, so a
    partial or unverifiable archive never becomes a chronicle entry.
    """
    key = s3.full_path(f"{OBSERVATION_PREFIX}/{sd}.parquet")
    try:
        as_of_poll, footer_rows = read_archive_meta(key, filesystem=fs)
    except FileNotFoundError:
        raise SystemExit(f"No raw archive for {sd} at {OBSERVATION_PREFIX}/{sd}.parquet")
    except Exception as exc:
        raise SystemExit(
            f"Could not read the {sd} archive footer ({type(exc).__name__}: {exc}). "
            "Refusing to backfill from an archive of unknown completeness."
        )

    routes: dict[str, dict] = {}
    stops: dict[str, dict] = {}
    rows = 0
    for batch in stream_observation(key, filesystem=fs):
        accumulate_totals(routes, stops, batch)
        rows += batch.num_rows

    if not rows:
        raise SystemExit(f"{sd} archive streamed 0 rows — nothing to backfill")
    if footer_rows is not None and rows != footer_rows:
        raise SystemExit(
            f"{sd} archive streamed {rows:,} rows but its footer claims "
            f"{footer_rows:,} — refusing to backfill a mismatched archive"
        )
    for label, totals in (("route", routes), ("stop", stops)):
        if _observations(totals) != rows:
            raise SystemExit(
                f"{sd} {label} totals sum to {_observations(totals):,} but the archive "
                f"holds {rows:,} rows — refusing to publish inconsistent totals"
            )
        if _categories(totals) != rows:
            raise SystemExit(
                f"{sd} {label} category counts sum to {_categories(totals):,} but the "
                f"archive holds {rows:,} rows — refusing to publish inconsistent totals"
            )

    daily = {
        "service_date": sd,
        "updated_at": datetime.now(EASTERN).isoformat(timespec="seconds"),
        "as_of_poll": as_of_poll,
        "routes": routes,
        "stops": stops,
    }
    return daily, rows


def _check_not_short(sd: str, rows: int, fs, min_rows: int | None) -> None:
    """Refuse an archive that looks short next to its neighbours."""
    if min_rows is not None:
        if rows < min_rows:
            raise SystemExit(
                f"{sd} archive holds {rows:,} rows, below --min-rows {min_rows:,} — "
                "refusing to backfill a short archive"
            )
        return

    neighbours = _neighbour_rows(sd, fs)
    if not neighbours:
        log("  no neighbouring archives readable — skipping the short-archive check")
        return
    floor = min(neighbours.values()) * SHORT_ARCHIVE_RATIO
    context = ", ".join(f"{d}={n:,}" for d, n in neighbours.items())
    if rows < floor:
        raise SystemExit(
            f"{sd} archive holds {rows:,} rows, far below its neighbours "
            f"({context}) — this looks like a known-short raw ledger, and a daily "
            f"built from it would bake the loss into the chronicle. Refusing; pass "
            f"--min-rows {rows} to override if this is expected."
        )
    log(f"  neighbours: {context} (short-archive floor {floor:,.0f}) — OK")


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def backfill(args: argparse.Namespace) -> int:
    sd = args.date
    try:
        date.fromisoformat(sd)
    except ValueError:
        raise SystemExit(f"{sd!r} is not a valid YYYY-MM-DD date")

    mode = "dry-run" if args.dry_run else "APPLY"
    log(f"[{mode}] backfill daily chronicle for {sd}")

    existing = s3.read_json(f"{DAILY_PREFIX}{sd}.json")
    if existing:
        total = _observations(existing.get("routes") or {})
        raise SystemExit(
            f"{DAILY_PREFIX}{sd}.json already exists ({total:,} observations) — "
            "refusing to overwrite a surviving chronicle entry"
        )
    log(f"  no existing {DAILY_PREFIX}{sd}.json on S3")

    fs = s3.filesystem()
    daily, rows = build_daily(sd, fs)
    _check_not_short(sd, rows, fs, args.min_rows)

    log(f"  archive rows: {rows:,}")
    log(f"  routes: {len(daily['routes']):,}   stops: {len(daily['stops']):,}")
    log(f"  as_of_poll: {daily['as_of_poll']}   updated_at: {daily['updated_at']}")

    local = STATE_DIR / "daily" / f"{sd}.json"
    if args.dry_run:
        log("\nDry run only — nothing was written.")
        return 0

    write_json(daily, local)
    log(f"  wrote {local}")

    if not s3.upload(f"{DAILY_PREFIX}{sd}.json", local):
        log(f"  WARNING upload of {DAILY_PREFIX}{sd}.json failed — local copy kept at {local}")
        return 1
    log(f"  uploaded {DAILY_PREFIX}{sd}.json")

    verify = s3.read_json(f"{DAILY_PREFIX}{sd}.json")
    if not verify:
        raise SystemExit(f"verification failed: {DAILY_PREFIX}{sd}.json not readable after upload")
    if _observations(verify.get("routes") or {}) != rows:
        raise SystemExit(
            f"verification failed: uploaded totals hold "
            f"{_observations(verify.get('routes') or {}):,} observations, expected {rows:,}"
        )
    if verify.get("as_of_poll") != daily["as_of_poll"]:
        raise SystemExit(
            f"verification failed: uploaded as_of_poll {verify.get('as_of_poll')} != "
            f"{daily['as_of_poll']}"
        )

    log(f"\nBackfilled {DAILY_PREFIX}{sd}.json ({rows:,} observations, verified).")
    log("Baseline and archives were not touched — this date is already folded.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    return backfill(args)


if __name__ == "__main__":
    raise SystemExit(main())