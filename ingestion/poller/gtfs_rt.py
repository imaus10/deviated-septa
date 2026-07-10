from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from google.transit import gtfs_realtime_pb2

BUS_TRIP_UPDATES = "https://www3.septa.org/gtfsrt/septa-pa-us/Trip/rtTripUpdates.pb"
BUS_VEHICLE_POSITIONS = (
    "https://www3.septa.org/gtfsrt/septa-pa-us/Vehicle/rtVehiclePosition.pb"
)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
EASTERN = ZoneInfo("America/New_York")


def _parse_time_str(time_str: str) -> tuple[int, int, int, int]:
    parts = time_str.split(":")
    h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    day_offset = h // 24
    h = h % 24
    return day_offset, h, m, s


def scheduled_to_ts(arrival_time: str, service_date: date) -> int:
    day_offset, h, m, s = _parse_time_str(arrival_time)
    dt = datetime(
        service_date.year, service_date.month, service_date.day, h, m, s,
        tzinfo=EASTERN,
    )
    dt += timedelta(days=day_offset)
    return int(dt.timestamp())


def fetch_protobuf(url: str) -> bytes:
    resp = httpx.get(url, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    return resp.content


def parse_trip_updates(raw: bytes) -> gtfs_realtime_pb2.FeedMessage:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(raw)
    return feed


def extract_observations(feed, stop_times_cache: dict) -> list[dict]:
    observations = []

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        tu = entity.trip_update
        trip_id = tu.trip.trip_id
        route_id = tu.trip.route_id
        direction_id = tu.trip.direction_id

        if tu.trip.schedule_relationship == gtfs_realtime_pb2.TripDescriptor.CANCELED:
            continue

        stop_times = stop_times_cache.get(trip_id)
        if not stop_times:
            continue

        predicted_ts_list = [
            stu.arrival.time
            for stu in tu.stop_time_update
            if stu.HasField("arrival") and stu.arrival.time > 0
        ]
        if not predicted_ts_list:
            continue

        predicted_ts_list.sort()
        median_ts = predicted_ts_list[len(predicted_ts_list) // 2]
        service_date = datetime.fromtimestamp(median_ts, tz=timezone.utc).date()

        vehicle_id = tu.vehicle.id if tu.vehicle.id else None

        for stu in tu.stop_time_update:
            if not (stu.HasField("arrival") and stu.arrival.time > 0):
                continue

            stop_seq = stu.stop_sequence
            predicted_ts = stu.arrival.time
            stop_id = stu.stop_id

            scheduled_row = stop_times.get(stop_seq)
            if scheduled_row is None:
                continue

            scheduled_ts = scheduled_to_ts(scheduled_row["arrival_time"], service_date)

            delay = int(predicted_ts) - scheduled_ts

            observations.append(
                {
                    "poll_timestamp": datetime.fromtimestamp(
                        feed.header.timestamp, tz=timezone.utc
                    ),
                    "trip_id": trip_id,
                    "route_id": route_id,
                    "direction_id": direction_id,
                    "stop_id": stop_id,
                    "stop_sequence": stop_seq,
                    "scheduled_time": datetime.fromtimestamp(
                        scheduled_ts, tz=timezone.utc
                    ),
                    "predicted_time": datetime.fromtimestamp(
                        int(predicted_ts), tz=timezone.utc
                    ),
                    "delay_seconds": delay,
                    "vehicle_id": vehicle_id,
                }
            )

    return observations


def load_stop_times(client, trip_ids: set[str]) -> dict:
    resp = client.post("/rpc/get_stop_times", json={"req_trip_ids": list(trip_ids)})
    resp.raise_for_status()
    rows = resp.json()

    cache: dict[str, dict[int, dict]] = {}
    for row in rows:
        tid = row["trip_id"]
        seq = row["stop_sequence"]
        if tid not in cache:
            cache[tid] = {}
        cache[tid][seq] = {
            "arrival_time": row["arrival_time"],
            "stop_id": row["stop_id"],
        }
    return cache


def build_aggregations(client):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    today_str = now.date().isoformat()

    resp = client.post("/rpc/agg_daily", json={"poll_date": today_str})
    resp.raise_for_status()
    print("  daily aggregation done")

    resp = client.post("/rpc/agg_hourly", json={"poll_date": today_str})
    resp.raise_for_status()
    print("  hourly aggregation done")

    resp = client.post("/rpc/agg_snapshot", json={"poll_date": today_str, "now": now.isoformat()})
    resp.raise_for_status()
    print("  snapshot done")
