#!/usr/bin/env python
"""Migrate Neon real_time_observations into the S3 parquet eternal ledger.

Streams observations from the Neon database per service date, resolves
route_id/stop_id/category, and writes one parquet per date via the archive
writer, then uploads to S3. Also seeds the routes/stops registries from the
current static feed.

Use --dry-run to preview without writing. Idempotent: a date already present on
S3 is skipped unless --overwrite. Use --date to process a single service date
(handy for a smoke test against the dev bucket).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import psycopg2

INGESTION_DIR = Path(__file__).resolve().parents[1]
if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

from dotenv import load_dotenv

import poller.archives as archives
import poller.s3 as s3
from poller.gtfs_rt import classify
from poller.gtfs_static import active_route_ids, load_local_metadata

load_dotenv(INGESTION_DIR.parent / ".env")

ROOT = INGESTION_DIR
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / "state"

OBSERVATION_S3_PREFIX = "archive/observations"
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"

OBSERVATION_QUERY = """
SELECT
    o.trip_id,
    o.stop_sequence,
    o.service_date,
    t.route_id,
    st.stop_id,
    o.delay_seconds,
    o.predicted_time,
    o.poll_timestamp,
    o.vehicle_id
FROM real_time_observations o
JOIN trips t        ON o.trip_id = t.trip_id
JOIN routes r       ON t.route_id = r.route_id
JOIN stop_times st  ON o.trip_id = st.trip_id
                   AND o.stop_sequence = st.stop_sequence
WHERE o.service_date = %s
  AND r.route_type IN (0, 3, 11)
ORDER BY o.trip_id, o.stop_sequence
"""


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate Neon real_time_observations into the S3 parquet ledger."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview what would be written/uploaded without doing it",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="re-write/re-upload dates already present on S3",
    )
    parser.add_argument(
        "--date",
        type=parse_date,
        help="process only this single YYYY-MM-DD service date",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _connect():
    url = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ["DATABASE_URL"]
    return psycopg2.connect(url, connect_timeout=10)


def list_service_dates(conn, only: date | None) -> list[date]:
    where = "WHERE service_date = %s" if only else ""
    params = (only,) if only else ()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT service_date FROM real_time_observations "
            f"{where} ORDER BY service_date",
            params,
        )
        return [row[0] for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

def fetch_date_rows(conn, service_date: date) -> list[tuple]:
    """Full store-row-shaped tuples for one day, in OBSERVATION_COLUMNS order."""
    rows = []
    with conn.cursor() as cur:
        cur.execute(OBSERVATION_QUERY, (service_date,))
        for trip_id, stop_seq, sd, route_id, stop_id, delay, predicted, polled, vehicle in cur:
            rows.append(
                (
                    trip_id,
                    stop_seq,
                    sd.isoformat(),
                    route_id,
                    stop_id,
                    delay,
                    classify(delay),
                    vehicle,
                    int(predicted.timestamp()),
                    int(polled.timestamp()),
                )
            )
    return rows


def migrate_observations(conn, args: argparse.Namespace) -> None:
    dates = list_service_dates(conn, args.date)
    if not dates:
        log("No service dates found.")
        return

    obs_dir = STATE_DIR / "archive" / "observations"
    mode = "dry-run" if args.dry_run else "APPLY"
    log(f"[{mode}] migrating {len(dates)} service date(s)")

    for service_date in dates:
        sd = service_date.isoformat()
        key = f"{OBSERVATION_S3_PREFIX}/{sd}.parquet"

        if not args.dry_run and not args.overwrite and s3.object_exists(key):
            log(f"  {sd}: already on S3, skipping (--overwrite to force)")
            continue

        rows = fetch_date_rows(conn, service_date)
        if not rows:
            log(f"  {sd}: no rows in scope, skipping")
            continue

        if args.dry_run:
            log(f"  {sd}: {len(rows)} rows -> {key}")
            continue

        path = archives.write_observations(rows, str(obs_dir))
        s3.upload_file(path, key, content_type=PARQUET_CONTENT_TYPE)
        path.unlink()
        log(f"  {sd}: {len(rows)} rows -> {key} uploaded + deleted")


# ---------------------------------------------------------------------------
# Registries (routes / stops)
# ---------------------------------------------------------------------------

def dropped_route_windows(conn, dropped_ids) -> dict[str, tuple[str, str]]:
    """Observation-derived validity windows for dropped routes.

    SEPTA keeps no public archive of past feeds, so a dropped route's real
    window is the span it was actually observed: (MIN(service_date),
    MAX(service_date)) from real_time_observations.
    """
    dropped_ids = list(dropped_ids)
    if not dropped_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.route_id, MIN(o.service_date), MAX(o.service_date) "
            "FROM real_time_observations o "
            "JOIN trips t ON o.trip_id = t.trip_id "
            "WHERE t.route_id = ANY(%s) "
            "GROUP BY t.route_id",
            (dropped_ids,),
        )
        return {
            rid: (min_sd.isoformat(), max_sd.isoformat())
            for rid, min_sd, max_sd in cur
        }


def seed_registries(conn, args: argparse.Namespace) -> None:
    data = load_local_metadata(str(DATA_DIR))
    active = active_route_ids(str(DATA_DIR))
    dropped = set(data["routes"]) - active
    windows = dropped_route_windows(conn, dropped)
    routes, stops = archives.build_registries(
        data,
        active_routes=active,
        route_windows=windows,
    )
    valid_from = archives._calendar_start(data)
    archive_dir = STATE_DIR / "archive"

    if args.dry_run:
        log(f"[dry-run] would seed {len(routes)} routes, {len(stops)} stops "
            f"(valid_from={valid_from}); {len(windows)} dropped-route windows: "
            f"{sorted(windows)}")
        return

    routes_path = archives.write_routes_registry(routes, str(archive_dir))
    s3.upload_file(routes_path, "archive/routes.parquet", content_type=PARQUET_CONTENT_TYPE)
    log(f"routes.parquet: {len(routes)} routes ({len(windows)} closed) -> uploaded (local kept)")

    stops_path = archives.write_stops_registry(stops, str(archive_dir))
    s3.upload_file(stops_path, "archive/stops.parquet", content_type=PARQUET_CONTENT_TYPE)
    log(f"stops.parquet: {len(stops)} stops -> uploaded (local kept)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    conn = _connect()
    try:
        seed_registries(conn, args)
        migrate_observations(conn, args)
    finally:
        conn.close()

    if args.dry_run:
        log("\nDry run only — nothing was written or uploaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
