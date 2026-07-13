from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from google.transit import gtfs_realtime_pb2

from poller.db import is_pg, json_dumps

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

            service_date = datetime.fromtimestamp(int(predicted_ts), tz=EASTERN).date()
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


def load_stop_times(db, trip_ids: set[str]) -> dict:
    if is_pg(db):
        return _load_stop_times_pg(db, trip_ids)
    return _load_stop_times_rest(db, trip_ids)


def _load_stop_times_rest(client, trip_ids):
    cache: dict[str, dict[int, dict]] = {}
    trip_list = list(trip_ids)
    batch_size = 7
    for i in range(0, len(trip_list), batch_size):
        batch = trip_list[i : i + batch_size]
        resp = client.post("/rpc/get_stop_times", json={"req_trip_ids": batch})
        resp.raise_for_status()
        rows = resp.json()
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


def _load_stop_times_pg(conn, trip_ids):
    cache: dict[str, dict[int, dict]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trip_id, stop_sequence, arrival_time, stop_id "
            "FROM stop_times WHERE trip_id = ANY(%s)",
            (list(trip_ids),),
        )
        for row in cur.fetchall():
            tid = row[0]
            seq = row[1]
            if tid not in cache:
                cache[tid] = {}
            cache[tid][seq] = {
                "arrival_time": row[2],
                "stop_id": row[3],
            }
    return cache


def upsert_arrival_records(db, observations):
    if is_pg(db):
        _upsert_arrival_records_pg(db, observations)
    else:
        _upsert_arrival_records_rest(db, observations)


def _upsert_arrival_records_rest(client, observations):
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


def _upsert_arrival_records_pg(conn, observations):
    cols = [
        "poll_timestamp", "trip_id", "route_id", "direction_id",
        "stop_id", "stop_sequence", "scheduled_time", "predicted_time",
        "delay_seconds", "vehicle_id",
    ]
    col_str = ", ".join(cols)

    from poller.db import DatetimeEncoder
    import json

    def _val(o, c):
        v = o[c]
        if isinstance(v, datetime):
            return v.isoformat()
        return v

    batch = []
    for obs in observations:
        batch.append(tuple(_val(obs, c) for c in cols))
        if len(batch) >= 1000:
            _insert_arrival_batch(conn, col_str, batch)
            batch.clear()
    if batch:
        _insert_arrival_batch(conn, col_str, batch)


def _insert_arrival_batch(conn, col_str, rows):
    from psycopg2.extras import execute_values

    sql = f"INSERT INTO arrival_records ({col_str}) VALUES %s"
    with conn.cursor() as cur:
        execute_values(cur, sql, rows)
    conn.commit()


def build_aggregations(db):
    now = datetime.now(timezone.utc)
    today_str = now.date().isoformat()

    if is_pg(db):
        _build_aggregations_pg(db, today_str, now)
    else:
        _build_aggregations_rest(db, today_str, now)


def _build_aggregations_rest(client, today_str, now):
    resp = client.post("/rpc/agg_daily", json={"poll_date": today_str})
    resp.raise_for_status()
    print("  daily aggregation done")

    resp = client.post("/rpc/agg_hourly", json={"poll_date": today_str})
    resp.raise_for_status()
    print("  hourly aggregation done")

    resp = client.post("/rpc/agg_snapshot", json={"poll_date": today_str, "now": now.isoformat()})
    resp.raise_for_status()
    print("  snapshot done")


def _build_aggregations_pg(conn, today_str, now):
    with conn.cursor() as cur:
        cur.execute("SELECT agg_daily(%s)", [today_str])
        conn.commit()
    print("  daily aggregation done")

    with conn.cursor() as cur:
        cur.execute("SELECT agg_hourly(%s)", [today_str])
        conn.commit()
    print("  hourly aggregation done")

    with conn.cursor() as cur:
        cur.execute("SELECT agg_snapshot(%s, %s)", [today_str, now.isoformat()])
        conn.commit()
    print("  snapshot done")
