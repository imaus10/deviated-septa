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

import poller.gtfs_rt as gtfs_rt
import poller.gtfs_static as gtfs_static
import poller.s3 as s3
from poller.constants import EASTERN
from poller.rollup import (
    build_current,
    prune_window,
    refresh_daily_chronicle,
    save_baseline,
    write_json,
)
from poller.state import ObservationsDB, save_state

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / "state"

load_dotenv(ROOT.parent / ".env")

CURRENT_CACHE_CONTROL = "max-age=55, stale-while-revalidate=5"


def _log_time(label, elapsed):
    print(f"  [{label}] {elapsed:.1f}s", flush=True)


def _eastern_today() -> str:
    return datetime.now(EASTERN).date().isoformat()


def _upload(key: str, path, **meta) -> None:
    """Best-effort upload with a single retry; local file always stays truth."""
    try:
        s3.upload_file(path, key, **meta)
    except Exception as e:
        try:
            s3.upload_file(path, key, **meta)
            return
        except Exception:
            print(f"  [s3] upload failed for {key}: {e}", flush=True)


def main():
    t0 = time.perf_counter()
    print(f"[{datetime.now(timezone.utc).isoformat()}] starting poll cycle", flush=True)

    # 1. Static data — download if freshness changed, else boot from local zip
    t1 = time.perf_counter()
    data, changed = gtfs_static.check_and_update(str(DATA_DIR))
    _log_time("static", time.perf_counter() - t1)
    if changed:
        print("  static feed refreshed", flush=True)

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
    observations = gtfs_rt.extract_observations(feed, data["stop_times"])

    rows = []
    for obs in observations:
        trip_id = obs["trip_id"]
        stop_seq = obs["stop_sequence"]
        trip = data["trips"].get(trip_id)
        scheduled = data["stop_times"].get((trip_id, stop_seq))
        if trip is None or scheduled is None:
            continue
        rows.append(
            {
                "trip_id": trip_id,
                "stop_sequence": stop_seq,
                "service_date": obs["service_date"],
                "route_id": trip["route_id"],
                "stop_id": scheduled["stop_id"],
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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = ObservationsDB(STATE_DIR / "observations.db")
    try:
        db.upsert(rows)

        present = {r["service_date"] for r in rows}
        store_dates = [sd for sd, _ in db.service_date_stats()]
        current_sd = (
            max(present).isoformat()
            if present
            else (store_dates[-1] if store_dates else _eastern_today())
        )
        print(f"  service date: {current_sd}", flush=True)

        baseline, pruned = prune_window(db, str(STATE_DIR), current_sd)
        if pruned:
            save_baseline(str(STATE_DIR), baseline)
            _upload("state/all-baseline.json", STATE_DIR / "all-baseline.json")
            print("  baseline rolled up for aged-out service dates", flush=True)

        for sd in refresh_daily_chronicle(db, str(STATE_DIR), current_sd):
            _upload(f"state/daily/{sd}.json", STATE_DIR / "daily" / f"{sd}.json")

        current = build_current(db, data, str(STATE_DIR), current_sd=current_sd)
        write_json(current, STATE_DIR / "current.json")
        _upload("public/current.json", STATE_DIR / "current.json", cache_control=CURRENT_CACHE_CONTROL)

        print(f"  observations total: {db.count()}", flush=True)
        save_state(str(STATE_DIR), current_sd, datetime.now(timezone.utc).timestamp())
    finally:
        db.close()

    _log_time("total", time.perf_counter() - t0)
    print(f"[{datetime.now(timezone.utc).isoformat()}] poll cycle complete", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)