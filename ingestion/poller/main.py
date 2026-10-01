"""One poll cycle — runs entirely locally (no database), rolls up four
periods, and pushes the public rollup to S3.

  1. Load/refresh GTFS static from the local zip
  2. Fetch GTFS-RT trip updates
  3. Extract observations, enrich with route/stop ids, UPSERT into SQLite
  4. Prune out-of-window service dates into the all-time baseline (fold once,
     drain the store incrementally), refresh the daily archive chronicle,
     upload changed archives
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

import gzip
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
from poller.constants import ARCHIVE_QUIET_WINDOW_MINUTES, EASTERN
from poller.rollup import (
    build_current,
    prune_window,
    refresh_daily_chronicle,
    save_baseline,
    write_json,
)
from poller.state import ObservationsDB, load_state, save_state, to_iso_date

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


def _archive_elapsed_dates(db, present, stats=None) -> None:
    """Archive elapsed store dates once they've gone quiet (capture-all).

    A date is archived only after it has been absent from the feed for
    ARCHIVE_QUIET_WINDOW_MINUTES (its store max poll is that old), so the full
    overnight straggler tail is captured instead of freezing at the midnight
    switchover. It is re-archived (overwrite) whenever a later straggler batch
    advances the store past the archive's footer as_of_poll — the eternal
    ledger ends up with every observation. Overwriting is fail-closed: an
    existing archive is only replaced when its footer read confirms the store
    is not mid-drain, and an unreadable footer is skipped, never overwritten.
    `stats` is an optional precomputed service_date_stats().
    """
    if not present:
        return
    present = {to_iso_date(d) for d in present}
    min_present = min(present)
    quiet_since = int(time.time()) - ARCHIVE_QUIET_WINDOW_MINUTES * 60
    obs_dir = STATE_DIR / "archive" / "observations"
    fs = s3.filesystem()
    for sd, max_poll in (stats or db.service_date_stats()):
        if sd >= min_present or not max_poll:
            continue
        if max_poll > quiet_since:
            continue  # still being updated — stragglers may keep arriving
        key = f"archive/observations/{sd}.parquet"
        try:
            archived_poll, archived_rows = archives.read_archive_meta(
                s3.full_path(key), filesystem=fs
            )
        except FileNotFoundError:
            archived_poll, archived_rows = None, None
        except Exception as e:
            # Footer unreadable (S3/WiFi hiccup, corrupt object, ...). We cannot
            # tell whether the existing archive is complete, so refuse to touch
            # it — the next cycle retries. Overwriting here is how 2026-09-08
            # lost rows: a transient read error was read as "nothing archived".
            print(
                f"  [archive] {key} skipped: footer unreadable "
                f"({type(e).__name__}: {e}) — refusing to overwrite",
                flush=True,
            )
            continue
        if archived_poll and max_poll <= archived_poll:
            continue  # already archived through this poll
        store_count = db.count(sd)
        if archived_rows is not None and store_count < archived_rows:
            # The store no longer holds the full day (the date is mid-drain).
            # Re-archiving from the partial store would overwrite a complete
            # archive with fewer rows — refuse; the straggler driving this is
            # garbage for a week-old date and gets drained anyway.
            print(
                f"  [archive] {key} skipped: store {store_count:,} < archive "
                f"{archived_rows:,} rows (mid-drain)",
                flush=True,
            )
            continue
        rows = db.export_day(sd)
        if not rows:
            continue
        path = archives.write_observations(rows, str(obs_dir), as_of_poll=max_poll)
        if s3.upload(key, path):
            path.unlink()
            print(f"  [archive] {key} uploaded (as_of_poll={max_poll})", flush=True)


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
        try:
            feed = gtfs_rt.fetch_trip_updates(gtfs_rt.BUS_TRIP_UPDATES)
        except gtfs_rt.FeedUnavailable as e:
            # Transient (WiFi/DNS/SEPTA hiccup) or truncated payload. Skip the
            # cycle cleanly: the store and current.json are untouched, so the
            # dashboard keeps serving the last good rollup. Next tick retries.
            print(f"  [rt] {e} — skipping this cycle", flush=True)
            return
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
        t = time.perf_counter()
        db.upsert(rows)
        _log_time("upsert", time.perf_counter() - t)

        present = {r["service_date"] for r in rows}
        t = time.perf_counter()
        store_dates = db.store_dates()
        current_sd = (
            max(present).isoformat()
            if present
            else (store_dates[-1] if store_dates else _eastern_today())
        )
        stats = db.service_date_stats()
        print(f"  service date: {current_sd}", flush=True)
        _log_time("store+stats", time.perf_counter() - t)

        t = time.perf_counter()
        _archive_elapsed_dates(db, present, stats)
        _log_time("archive", time.perf_counter() - t)

        t = time.perf_counter()
        baseline, pruned = prune_window(db, str(STATE_DIR), current_sd, stats=stats)
        if pruned:
            save_baseline(str(STATE_DIR), baseline)
            s3.upload("state/all-baseline.json", STATE_DIR / "all-baseline.json")
            print("  baseline rolled up for aged-out service dates", flush=True)
        _log_time("prune", time.perf_counter() - t)

        t = time.perf_counter()
        rewritten = refresh_daily_chronicle(
            db, str(STATE_DIR), current_sd, stats=stats,
            folded_through=baseline.get("max_service_date"),
        )
        for sd in rewritten:
            s3.upload(f"state/daily/{sd}.json", STATE_DIR / "daily" / f"{sd}.json")
        _log_time("daily refresh", time.perf_counter() - t)

        t_rollup = time.perf_counter()
        current = build_current(db, metadata, str(STATE_DIR), current_sd=current_sd)
        _log_time("rollup", time.perf_counter() - t_rollup)

        t = time.perf_counter()
        write_json(current, STATE_DIR / "current.json")
        _log_time("write", time.perf_counter() - t)

        t = time.perf_counter()
        # The S3 object is gzip-compressed + served with Content-Encoding: gzip,
        # so browsers transparently decode it on fetch (no frontend change). The
        # local current.json stays raw (source of truth). mtime=0 -> deterministic.
        gz_path = STATE_DIR / "current.json.gz"
        gz_path.write_bytes(gzip.compress((STATE_DIR / "current.json").read_bytes(), mtime=0))
        s3.upload("public/current.json", gz_path,
                  cache_control=CURRENT_CACHE_CONTROL, content_encoding="gzip")
        gz_path.unlink()
        _log_time("upload", time.perf_counter() - t)

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