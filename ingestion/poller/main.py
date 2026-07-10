import sys
from datetime import datetime, timezone

import poller.gtfs_rt as gtfs_rt
import poller.gtfs_static as gtfs_static
from poller.db import get_client, json_dumps


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] starting poll cycle", flush=True)

    client = get_client()

    # Only import static data once — routes table is small and quick to check.
    today = datetime.now(timezone.utc).date()
    try:
        resp = client.get("/routes", params={"select": "route_id", "limit": 1})
        resp.raise_for_status()
        static_loaded = len(resp.json()) > 0
    except Exception:
        static_loaded = False

    if not static_loaded:
        print("no static data found; importing GTFS static data", flush=True)
        gtfs_static.run(client)

    print("fetching trip updates...", flush=True)
    raw = gtfs_rt.fetch_protobuf(gtfs_rt.BUS_TRIP_UPDATES)
    feed = gtfs_rt.parse_trip_updates(raw)

    # Collect unique trip_ids from the feed
    trip_ids = {
        e.trip_update.trip.trip_id
        for e in feed.entity
        if e.HasField("trip_update")
        and e.trip_update.trip.schedule_relationship
        != gtfs_rt.gtfs_realtime_pb2.TripDescriptor.CANCELED
    }

    if not trip_ids:
        print("no active trips in feed", flush=True)
        client.close()
        return

    print(f"  {len(trip_ids)} active trips in feed", flush=True)

    stop_cache = gtfs_rt.load_stop_times(client, trip_ids)
    if not stop_cache:
        print("no matching stop_times found; static data may need refresh", flush=True)
        client.close()
        return

    observations = gtfs_rt.extract_observations(feed, stop_cache)
    print(f"  {len(observations)} observations extracted", flush=True)

    if observations:
        batch = []
        for obs in observations:
            batch.append(obs)
            if len(batch) >= 1000:
                resp = client.post("/arrival_records", content=json_dumps(batch),
                                   headers={"Prefer": "return=minimal"})
                resp.raise_for_status()
                batch.clear()
        if batch:
            resp = client.post("/arrival_records", content=json_dumps(batch),
                               headers={"Prefer": "return=minimal"})
            resp.raise_for_status()

        print("  running aggregations...", flush=True)
        gtfs_rt.build_aggregations(client)

    print(f"[{datetime.now(timezone.utc).isoformat()}] poll cycle complete", flush=True)
    client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
