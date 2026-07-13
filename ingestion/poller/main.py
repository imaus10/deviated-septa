import sys
from datetime import datetime, timezone

import poller.gtfs_rt as gtfs_rt
import poller.gtfs_static as gtfs_static
from poller.db import get_client, get_connection, is_pg


def main():
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

    try:
        if not gtfs_static.is_static_loaded(db):
            print("no static data found; importing GTFS static data", flush=True)
            gtfs_static.run(db)

        print("fetching trip updates...", flush=True)
        raw = gtfs_rt.fetch_protobuf(gtfs_rt.BUS_TRIP_UPDATES)
        feed = gtfs_rt.parse_trip_updates(raw)

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

        stop_cache = gtfs_rt.load_stop_times(db, trip_ids)
        if not stop_cache:
            print("no matching stop_times found; static data may need refresh", flush=True)
            return

        observations = gtfs_rt.extract_observations(feed, stop_cache)
        print(f"  {len(observations)} observations extracted", flush=True)

        if observations:
            gtfs_rt.upsert_arrival_records(db, observations)

            print("  running aggregations...", flush=True)
            gtfs_rt.build_aggregations(db)

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
