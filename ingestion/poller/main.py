import sys
import time
from datetime import datetime, timezone

import poller.gtfs_rt as gtfs_rt
import poller.gtfs_static as gtfs_static
from poller.db import get_client, get_connection


def _log_time(label, elapsed):
    print(f"  [{label}] {elapsed:.1f}s", flush=True)


def main():
    t0 = time.perf_counter()
    print(f"[{datetime.now(timezone.utc).isoformat()}] starting poll cycle", flush=True)

    conn = None
    client = None

    try:
        conn = get_connection()
    except Exception:
        pass

    if conn:
        db = conn
        print("  connected via direct Postgres", flush=True)
    else:
        client = get_client()
        db = client
        print("  connected via Supabase REST API", flush=True)
    _log_time("connect", time.perf_counter() - t0)

    try:
        t1 = time.perf_counter()
        if not gtfs_static.is_static_loaded(db):
            print("no static data found; importing GTFS static data", flush=True)
            gtfs_static.run(db)
        _log_time("static check", time.perf_counter() - t1)

        t2 = time.perf_counter()
        print("fetching trip updates...", flush=True)
        raw = gtfs_rt.fetch_protobuf(gtfs_rt.BUS_TRIP_UPDATES)
        feed = gtfs_rt.parse_trip_updates(raw)
        _log_time("fetch + parse", time.perf_counter() - t2)

        trip_ids = {
            e.trip_update.trip.trip_id
            for e in feed.entity
            if e.HasField("trip_update")
            and e.trip_update.trip.schedule_relationship
            != gtfs_rt.gtfs_realtime_pb2.TripDescriptor.CANCELED
        }

        if not trip_ids:
            print("no active trips in feed", flush=True)
            return

        print(f"  {len(trip_ids)} active trips in feed", flush=True)

        t3 = time.perf_counter()
        stop_cache = gtfs_rt.load_stop_times(db, trip_ids)
        if not stop_cache:
            print("no matching stop_times found; static data may need refresh", flush=True)
            return
        _log_time("load stop_times", time.perf_counter() - t3)

        t4 = time.perf_counter()
        observations = gtfs_rt.extract_observations(feed, stop_cache)
        _log_time("extract observations", time.perf_counter() - t4)
        print(f"  {len(observations)} observations extracted", flush=True)

        if observations:
            t5 = time.perf_counter()
            gtfs_rt.upsert_arrival_records(db, observations)
            _log_time("insert arrival_records", time.perf_counter() - t5)

            t6 = time.perf_counter()
            print("  running aggregations...", flush=True)
            gtfs_rt.build_aggregations(db)
            _log_time("aggregations", time.perf_counter() - t6)

        _log_time("total", time.perf_counter() - t0)
        print(f"[{datetime.now(timezone.utc).isoformat()}] poll cycle complete", flush=True)

    finally:
        if conn:
            conn.close()
        if client:
            client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
