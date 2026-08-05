import sys
import time
from datetime import datetime, timezone

import poller.gtfs_rt as gtfs_rt
import poller.gtfs_static as gtfs_static
from poller.db import get_connection


def _log_time(label, elapsed):
    print(f"  [{label}] {elapsed:.1f}s", flush=True)


def main():
    t0 = time.perf_counter()
    print(f"[{datetime.now(timezone.utc).isoformat()}] starting poll cycle", flush=True)

    conn = get_connection()
    print("  connected via direct Postgres", flush=True)
    _log_time("connect", time.perf_counter() - t0)

    try:
        t1 = time.perf_counter()
        last_modified = gtfs_static.get_freshness()
        if last_modified != gtfs_static.get_stored_freshness(conn):
            print("static data updated; re-importing", flush=True)
            gtfs_static.run_and_record_freshness(conn)
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
        stop_cache = gtfs_rt.load_stop_times(conn, trip_ids)
        matched = len(stop_cache)
        missing = sorted(trip_ids - stop_cache.keys())

        if not stop_cache:
            print(
                f"  [coverage] 0/{len(trip_ids)} trips matched static; "
                "NONE found, static data may be stale",
                flush=True,
            )
            return
        if missing:
            sample = ", ".join(missing[:10])
            extra = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
            print(
                f"  [coverage] {matched}/{len(trip_ids)} trips matched static; "
                f"{len(missing)} MISSING: {sample}{extra}",
                flush=True,
            )
        else:
            print(
                f"  [coverage] {matched}/{len(trip_ids)} trips matched static",
                flush=True,
            )
        _log_time("load stop_times", time.perf_counter() - t3)

        t4 = time.perf_counter()
        observations = gtfs_rt.extract_observations(feed, stop_cache)
        _log_time("extract observations", time.perf_counter() - t4)
        print(f"  {len(observations)} observations extracted", flush=True)

        if observations:
            t5 = time.perf_counter()
            gtfs_rt.update_predictions(conn, observations)
            _log_time("update predictions", time.perf_counter() - t5)

            t6 = time.perf_counter()
            print("  running aggregations...", flush=True)
            gtfs_rt.build_aggregations(conn)
            _log_time("aggregations", time.perf_counter() - t6)

        _log_time("total", time.perf_counter() - t0)
        print(f"[{datetime.now(timezone.utc).isoformat()}] poll cycle complete", flush=True)

    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
