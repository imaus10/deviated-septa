#!/usr/bin/env python
"""One-shot cutover: prepare the Pi to run the local+S3 poller from Neon history.

Run ON THE PI with the old poller cron stopped:

    1. stop the cron (comment out / remove the poller line)
    2. uv run python -m scripts.cutover --apply --env-file <repo-root>/.env.prod
    3. re-enable the cron

Phases:
  0 preflight  — env vars present, S3 archive inventory, today detection
  1 static     — ensure local GTFS zip + state/static.db; always emit + upload
                 public/geometries.json (re-seed registries if the feed changed)
  2 today      — delete the partial `archive/observations/<today>.parquet` from S3
                 so the poller finalizes today at service-date switchover (the
                 poller skips dates already on S3, so a mid-day migrate snapshot
                 would otherwise be frozen forever)
  3 restore    — wipe local state, rebuild store window + baseline + daily +
                 current.json from the S3 ledger (restore_state.restore)
  4 verify     — HEAD the S3 artifacts; optionally run one live poll cycle
  5 summary

Dry-run by default (--apply to execute). Nothing is written, deleted, or
uploaded in dry-run mode. No SSH orchestration and no cron changes here —
the operator stops/starts cron by hand around the script.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

INGESTION_DIR = Path(__file__).resolve().parents[1]
if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

import poller.archives as archives
import poller.gtfs_static as gtfs_static
import poller.route_geometries as route_geometries
import poller.s3 as s3
import scripts.restore_state as restore_state
from poller.constants import EASTERN
from poller.rollup import write_json

ROOT = INGESTION_DIR
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / "state"
STATIC_DB = STATE_DIR / "static.db"

CURRENT_CACHE_CONTROL = "max-age=55, stale-while-revalidate=5"
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"


def log(message: str = "") -> None:
    print(message, flush=True)


def _eastern_today() -> str:
    return datetime.now(EASTERN).date().isoformat()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cut the Pi over to the local+S3 poller from Neon history."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="execute the cutover (default is a dry run)",
    )
    parser.add_argument(
        "--env-file",
        default=str(INGESTION_DIR.parent / ".env"),
        help="dotenv file with the S3_* vars (default: repo-root .env)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the delete-today-partial confirmation prompt",
    )
    parser.add_argument(
        "--skip-poll",
        action="store_true",
        help="skip the live verification poll in the verify phase",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------

def load_env(env_file: str) -> None:
    path = Path(env_file)
    if not path.exists():
        raise SystemExit(f"env file not found: {path}")
    load_dotenv(path, override=True)


# ---------------------------------------------------------------------------
# Phase 0 — preflight
# ---------------------------------------------------------------------------

def _archive_dates() -> list[str]:
    keys = s3.list_objects("archive/observations/")
    return sorted(k.split("/")[-1][:10] for k in keys if k.endswith(".parquet"))


def preflight(args) -> list[str]:
    missing = [k for k in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
               if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing env vars ({', '.join(missing)}) — check --env-file")

    dates = _archive_dates()
    if not dates:
        raise SystemExit("no archive/observations/*.parquet found on S3 — run the "
                         "Neon migration first (or check --env-file / bucket)")
    today = _eastern_today()
    log(f"[preflight] bucket={os.environ['S3_BUCKET']} archives={len(dates)} "
        f"range={dates[0]}..{dates[-1]}")
    if dates[-1] >= today:
        log(f"[preflight] NOTE: {dates[-1]} is today or newer — its S3 parquet is a "
            f"partial snapshot; phase 2 will drop it so the poller can finalize it")
    return dates


# ---------------------------------------------------------------------------
# Phase 1 — static bootstrap + geometries
# ---------------------------------------------------------------------------

def _seed_registries(metadata) -> None:
    routes, stops = archives.build_registries(metadata)
    archive_dir = STATE_DIR / "archive"
    routes_path = archives.write_routes_registry(routes, str(archive_dir))
    s3.upload("archive/routes.parquet", routes_path, content_type=PARQUET_CONTENT_TYPE)
    stops_path = archives.write_stops_registry(stops, str(archive_dir))
    s3.upload("archive/stops.parquet", stops_path, content_type=PARQUET_CONTENT_TYPE)
    log(f"[static] registries: {len(routes)} routes, {len(stops)} stops uploaded")


def bootstrap_static(args) -> None:
    if not args.apply:
        zip_ok = (DATA_DIR / "latest.zip").exists()
        db_ok = STATIC_DB.exists()
        log(f"[static] dry-run: latest.zip={'present' if zip_ok else 'MISSING'}, "
            f"static.db={'present' if db_ok else 'MISSING'}")
        log("[static] dry-run: would ensure the GTFS zip, (re)build state/static.db, "
            "and build + upload public/geometries.json")
        return

    static, changed = gtfs_static.check_and_update(str(DATA_DIR), str(STATIC_DB))
    try:
        metadata = gtfs_static.load_local_metadata(str(DATA_DIR))
        if changed:
            _seed_registries(metadata)
            log("[static] feed changed -> re-seeded registries")

        geometries = route_geometries.build_geometries(static, metadata)
        geo_path = STATE_DIR / "geometries.json"
        write_json(geometries, geo_path)
        s3.upload("public/geometries.json", geo_path, cache_control=CURRENT_CACHE_CONTROL)
        log(f"[static] {len(geometries)} routes -> public/geometries.json uploaded")
    finally:
        static.close()


# ---------------------------------------------------------------------------
# Phase 2 — drop today's partial archive
# ---------------------------------------------------------------------------

def drop_today_partial(args, today: str) -> None:
    key = f"archive/observations/{today}.parquet"
    if not s3.object_exists(key):
        log(f"[today] {key} not on S3 — nothing to drop")
        return
    if not args.apply:
        log(f"[today] dry-run: would DELETE {key} (partial today snapshot, so the "
            f"poller can finalize today at switchover)")
        return
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("refusing to delete the partial today archive without "
                             "--yes in non-interactive mode")
        answer = input(f"  Delete {key} from S3? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            raise SystemExit("aborted: keeping today's partial archive")
    s3.delete_object(key)
    log(f"[today] deleted {key} (poller will finalize today at switchover)")


# ---------------------------------------------------------------------------
# Phase 3 — restore from the S3 ledger
# ---------------------------------------------------------------------------

def _restore_ns(dry_run: bool) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.dry_run = dry_run
    ns.date = None
    return ns


def restore(args) -> None:
    if not args.apply:
        log("[restore] dry-run: would wipe observations.db + all-baseline.json + "
            "daily/, then rebuild the store window + baseline + current.json from S3")
        restore_state.restore(_restore_ns(dry_run=True))
        return

    # Wipe stale local state. observations.db is also unlinked by restore itself,
    # but baseline + daily MUST go first: restore folds the S3 ledger on top of an
    # existing baseline, which would double-count dates already pruned locally.
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{STATE_DIR / 'observations.db'}{suffix}")
        if p.exists():
            p.unlink()
    baseline_path = STATE_DIR / "all-baseline.json"
    if baseline_path.exists():
        baseline_path.unlink()
    daily_dir = STATE_DIR / "daily"
    if daily_dir.is_dir():
        for p in daily_dir.glob("*.json"):
            p.unlink()
    log("[restore] wiped observations.db, all-baseline.json, daily/")

    restore_state.restore(_restore_ns(dry_run=False))


# ---------------------------------------------------------------------------
# Phase 4 — verify
# ---------------------------------------------------------------------------

def verify(args, dates: list[str]) -> None:
    if not args.apply:
        log("[verify] dry-run: would HEAD public/current.json, public/geometries.json, "
            "registries, sample archives"
            + ("" if args.skip_poll else ", and run one live poll cycle"))
        return

    for key in ("public/current.json", "public/geometries.json",
                "archive/routes.parquet", "archive/stops.parquet"):
        present = s3.object_exists(key)
        log(f"[verify] {key}: {'OK' if present else 'MISSING'}")
        if not present:
            raise SystemExit(f"[verify] {key} missing from S3 — cutover incomplete")

    today = _eastern_today()
    samples = [dates[0]]
    if dates[-1] != today:
        samples.append(dates[-1])
    elif len(dates) > 1:
        samples.append(dates[-2])
    for d in samples:
        key = f"archive/observations/{d}.parquet"
        log(f"[verify] {key}: {'OK' if s3.object_exists(key) else 'MISSING'}")

    today_key = f"archive/observations/{today}.parquet"
    if s3.object_exists(today_key):
        log(f"[verify] {today_key}: PRESENT (unexpected — today should have been dropped)")
    else:
        log(f"[verify] {today_key}: absent (expected — poller will finalize today)")

    if args.skip_poll:
        return
    log("[verify] running one live poll cycle...")
    import poller.main as poller_main
    poller_main.main()
    log("[verify] poll cycle OK")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def summary() -> None:
    log("\nCutover complete. Re-enable the poller cron on the Pi now.")
    log("Next steps: frontend cutover (VITE_DATA_BASE_URL), then Neon deprovision.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    load_env(args.env_file)

    mode = "APPLY" if args.apply else "DRY-RUN"
    log(f"=== deviated-septa cutover ({mode}) ===")
    dates = preflight(args)
    bootstrap_static(args)
    drop_today_partial(args, _eastern_today())
    restore(args)
    verify(args, dates)
    if args.apply:
        summary()
    else:
        log("\nDry run only — nothing was written, deleted, or uploaded. "
            "Re-run with --apply to execute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())