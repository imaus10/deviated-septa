"""One poll cycle — runs entirely locally (no database).

  1. Load/refresh GTFS static from the local zip
  2. Fetch GTFS-RT trip updates
  3. Extract observations, enrich with route/stop ids, UPSERT into SQLite
  4. Build and write the current-day rollup (state/current.json)
  5. Persist state.json (service date + last poll time)

Day closeout (daily JSON, parquet archive, weekly/all recompute) and S3
upload are handled by the storage phase.
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import poller.gtfs_rt as gtfs_rt
import poller.gtfs_static as gtfs_static
from poller.constants import EASTERN
from poller.rollup import build_rollup, write_current
from poller.state import ObservationsDB, load_state, save_state

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / "state"


def _log_time(label, elapsed):
    print(f"  [{label}] {elapsed:.1f}s", flush=True)


def _eastern_today() -> str:
    return datetime.now(EASTERN).date().isoformat()


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

    # 4. Advance service date, then persist to SQLite + rollup
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state(str(STATE_DIR))
    today = _eastern_today()

    if state.get("service_date") and state["service_date"] != today:
        # Day closeout (daily JSON, parquet archive, weekly/all) is handled
        # by the storage phase; prior-day rows are retained until then.
        print(f"  service date rollover: {state['service_date']} -> {today}", flush=True)

    db = ObservationsDB(STATE_DIR / "observations.db")
    try:
        db.upsert(rows)
        print(f"  observations total: {db.count()}", flush=True)

        rollup = build_rollup(db, today, data)
        write_current(rollup, str(STATE_DIR))
        save_state(str(STATE_DIR), today, datetime.now(timezone.utc).timestamp())
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