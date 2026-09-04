"""One poll cycle — runs entirely locally (no database), rolls up four
periods, and pushes the public rollup to S3.

  1. Load/refresh GTFS static from the local zip
  2. Fetch GTFS-RT trip updates
  3. Extract observations, enrich with route/stop ids, UPSERT into SQLite
  4. Prune out-of-window service dates into the all-time baseline (fold +
     delete), refresh the daily archive chronicle, upload changed archives
  5. Build the 4-period current.json → write locally → S3 public/current.json
  6. Persist state.json (service date + last poll time)

Period semantics are data-driven, never wall-clock: current_service_date is
the newest service date seen in the feed; 'week' reads the SQLite store over
the last 7 service dates; 'all' = all-time baseline + whatever the store
still holds. The store keeps only the 7-date window, so local disk stays
bounded; S3 (state/daily/, state/all-baseline.json) is the eternal chronicle.

Parquet raw archives, GTFS-static snapshots, and geometries.json are for
later phases. Local state/ files are always the source of truth; S3 uploads
are best-effort (warn, never crash the cycle).
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import poller.archives as archives
import poller.gtfs_rt as gtfs_rt
import poller.gtfs_static as gtfs_static
import poller.route_geometries as route_geometries
import poller.s3 as s3
from poller.constants import EASTERN
from poller.rollup import (
    build_current,
    prune_window,
    refresh_daily_chronicle,
    save_baseline,
    write_json,
)
from poller.state import ObservationsDB, load_state, save_state

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / "state"
STATIC_DB = STATE_DIR / "static.db"

load_dotenv(ROOT.parent / ".env")

CURRENT_CACHE_CONTROL = "max-age=55, stale-while-revalidate=5"
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"


def _log_time(label, elapsed):
    print(f"  [{label}] {elapsed:.1f}s", flush=True)


def _eastern_today() -> str:
    return datetime.now(EASTERN).date().isoformat()


def _archive_elapsed_dates(db, current_sd) -> None:
    """Archive every store service date strictly before current_sd.

    Fires once a new service day starts: dates fully elapsed (older than the
    current service date) are written to archive/observations/<sd>.parquet,
    uploaded, then deleted locally (S3 is the sole copy / eternal ledger).

    A date already present at archive/observations/<sd>.parquet on S3 is
    skipped to avoid re-uploading a finalized archive. On a successful upload
    the local parquet is removed; on a failure it is kept so the next cycle
    retries.
    """
    obs_dir = STATE_DIR / "archive" / "observations"
    for sd, _ in db.service_date_stats():
        if sd >= current_sd:
            continue
        key = f"archive/observations/{sd}.parquet"
        if s3.object_exists(key):
            continue
        rows = db.export_day(sd)
        if not rows:
            continue
        path = archives.write_observations(rows, str(obs_dir))
        if s3.upload(key, path):
            path.unlink()
            print(f"  [archive] {key} uploaded + deleted", flush=True)


def _existing_registry(key: str) -> dict:
    """Registry rows already on S3 ({} if none yet)."""
    if not s3.object_exists(key):
        return {}
    return archives.read_registry(s3.full_path(key), filesystem=s3.filesystem())


def _refresh_static_derived(metadata, static, db) -> None:
    """Regenerate static-derived artifacts after a fresh static feed import.

    Emits public/geometries.json plus the route/stop registries, and uploads
    all three to S3. Registries are consolidated over the existing S3 ledger:
    present routes stay open-ended, newly-dropped routes (in routes.txt but no
    longer in trips) are closed with their newest store service date, and
    previously-closed rows are preserved — never overwritten/reopened.
    """
    geometries = route_geometries.build_geometries(static, metadata)
    geo_path = STATE_DIR / "geometries.json"
    write_json(geometries, geo_path)
    s3.upload("public/geometries.json", geo_path, cache_control=CURRENT_CACHE_CONTROL)
    print(f"  [geometries] {len(geometries)} routes -> uploaded", flush=True)

    active_routes = {rid for _trip_id, rid in static.iter_trips()}
    existing_routes = _existing_registry("archive/routes.parquet")
    dropped = set(metadata["routes"]) - active_routes
    route_windows = {
        rid: (None, sd)
        for rid, sd in db.last_service_date_for_routes(dropped).items()
    }
    routes, stops = archives.build_registries(
        metadata,
        active_routes=active_routes,
        existing_routes=existing_routes,
        route_windows=route_windows,
        existing_stops=_existing_registry("archive/stops.parquet"),
    )
    archive_dir = STATE_DIR / "archive"
    routes_path = archives.write_routes_registry(routes, str(archive_dir))
    s3.upload("archive/routes.parquet", routes_path, content_type=PARQUET_CONTENT_TYPE)
    stops_path = archives.write_stops_registry(stops, str(archive_dir))
    s3.upload("archive/stops.parquet", stops_path, content_type=PARQUET_CONTENT_TYPE)
    print(
        f"  [registries] {len(routes)} routes ({len(route_windows)} closed), "
        f"{len(stops)} stops -> uploaded",
        flush=True,
    )



def main():
    t0 = time.perf_counter()
    print(f"[{datetime.now(timezone.utc).isoformat()}] starting poll cycle", flush=True)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = None
    static = None
    try:
        db = ObservationsDB(STATE_DIR / "observations.db")

        # 1. Static data — download if freshness changed, else boot from local zip
        t1 = time.perf_counter()
        static, changed = gtfs_static.check_and_update(str(DATA_DIR), str(STATIC_DB))
        metadata = gtfs_static.load_local_metadata(str(DATA_DIR))
        _log_time("static", time.perf_counter() - t1)
        if changed:
            print("  static feed refreshed", flush=True)
            _refresh_static_derived(metadata, static, db)

    # 2. Fetch + parse the RT feed
        t2 = time.perf_counter()
        print("fetching trip updates...", flush=True)
        raw = gtfs_rt.fetch_protobuf(gtfs_rt.BUS_TRIP_UPDATES)
        feed = gtfs_rt.parse_trip_updates(raw)
        _log_time("fetch + parse", time.perf_counter() - t2)

        active_trips = {
            e.trip_update.trip.trip_id
            for e in feed.entity
            if e.HasField("trip_update")
            and e.trip_update.trip.schedule_relationship
            != gtfs_rt.gtfs_realtime_pb2.TripDescriptor.CANCELED
        }

        # 3. Extract observations and enrich with route/stop ids + category
        t3 = time.perf_counter()
        observations = gtfs_rt.extract_observations(feed, static)

        rows = []
        for obs in observations:
            trip_id = obs["trip_id"]
            stop_seq = obs["stop_sequence"]
            route_id = static.route_for_trip(trip_id)
            if route_id is None:
                continue
            rows.append(
                {
                    "trip_id": trip_id,
                    "stop_sequence": stop_seq,
                    "service_date": obs["service_date"],
                    "route_id": route_id,
                    "stop_id": obs["stop_id"],
                    "delay_seconds": obs["delay_seconds"],
                    "category": gtfs_rt.classify(obs["delay_seconds"]),
                    "vehicle_id": obs.get("vehicle_id"),
                    "predicted_time": obs["predicted_time"],
                    "poll_timestamp": obs["poll_timestamp"],
                }
            )

        matched = {r["trip_id"] for r in rows}
        missing = sorted(active_trips - matched)
        if active_trips and missing:
            sample = ", ".join(missing[:10])
            extra = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
            print(
                f"  [coverage] {len(matched)}/{len(active_trips)} trips matched static; "
                f"{len(missing)} MISSING: {sample}{extra}",
                flush=True,
            )
        else:
            print(
                f"  [coverage] {len(matched)}/{len(active_trips)} trips matched static",
                flush=True,
            )
        _log_time("extract observations", time.perf_counter() - t3)
        print(f"  {len(rows)} observations extracted", flush=True)

    # 4. Persist: prune the window, refresh the chronicle, roll up current.json
        db.upsert(rows)

        present = {r["service_date"] for r in rows}
        store_dates = db.store_dates()
        current_sd = (
            max(present).isoformat()
            if present
            else (store_dates[-1] if store_dates else _eastern_today())
        )
        print(f"  service date: {current_sd}", flush=True)

        _archive_elapsed_dates(db, current_sd)

        baseline, pruned = prune_window(db, str(STATE_DIR), current_sd)
        if pruned:
            save_baseline(str(STATE_DIR), baseline)
            s3.upload("state/all-baseline.json", STATE_DIR / "all-baseline.json")
            print("  baseline rolled up for aged-out service dates", flush=True)

        for sd in refresh_daily_chronicle(db, str(STATE_DIR), current_sd):
            s3.upload(f"state/daily/{sd}.json", STATE_DIR / "daily" / f"{sd}.json")

        t_rollup = time.perf_counter()
        current = build_current(db, metadata, str(STATE_DIR), current_sd=current_sd)
        _log_time("rollup", time.perf_counter() - t_rollup)
        write_json(current, STATE_DIR / "current.json")
        s3.upload("public/current.json", STATE_DIR / "current.json", cache_control=CURRENT_CACHE_CONTROL)

        print(f"  observations total: {db.count()}", flush=True)
        save_state(str(STATE_DIR), current_sd, datetime.now(timezone.utc).timestamp())
    finally:
        if db is not None:
            db.close()
        if static is not None:
            static.close()

    _log_time("total", time.perf_counter() - t0)
    print(f"[{datetime.now(timezone.utc).isoformat()}] poll cycle complete", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)