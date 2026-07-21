import logging
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from google.transit import gtfs_realtime_pb2

from poller.db import is_pg, json_dumps

log = logging.getLogger(__name__)

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

            scheduled_row = stop_times.get(stop_seq)
            if scheduled_row is None:
                continue

            arrival = scheduled_row.get("arrival_time")
            if not arrival:
                log.warning("skip trip=%s seq=%s: empty arrival_time", trip_id, stop_seq)
                continue

            service_date = datetime.fromtimestamp(int(predicted_ts), tz=EASTERN).date()
            scheduled_ts = scheduled_to_ts(arrival, service_date)

            delay = int(predicted_ts) - scheduled_ts

            observations.append(
                {
                    "trip_id": trip_id,
                    "stop_sequence": stop_seq,
                    "predicted_time": datetime.fromtimestamp(
                        int(predicted_ts), tz=timezone.utc
                    ),
                    "delay_seconds": delay,
                    "vehicle_id": vehicle_id,
                    "poll_timestamp": datetime.fromtimestamp(
                        feed.header.timestamp, tz=timezone.utc
                    ),
                    "service_date": service_date,
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


def update_predictions(db, observations):
    if is_pg(db):
        _update_predictions_pg(db, observations)
    else:
        _update_predictions_rest(db, observations)


def _update_predictions_rest(client, observations):
    batch = []
    for obs in observations:
        batch.append(obs)
        if len(batch) >= 1000:
            resp = client.post("/real_time_observations", content=json_dumps(batch),
                               headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
            resp.raise_for_status()
            batch.clear()
    if batch:
        resp = client.post("/real_time_observations", content=json_dumps(batch),
                           headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        resp.raise_for_status()


def _update_predictions_pg(conn, observations):
    import io

    cols = [
        "trip_id", "stop_sequence",
        "predicted_time", "delay_seconds", "vehicle_id", "poll_timestamp",
        "service_date",
    ]

    buf = io.StringIO()
    for obs in observations:
        row = [
            str(obs["trip_id"]),
            str(obs["stop_sequence"]),
            obs["predicted_time"].isoformat() if obs["predicted_time"] else "\\N",
            str(obs["delay_seconds"]) if obs["delay_seconds"] is not None else "\\N",
            str(obs["vehicle_id"]) if obs["vehicle_id"] else "\\N",
            obs["poll_timestamp"].isoformat() if obs["poll_timestamp"] else "\\N",
            str(obs["service_date"]) if obs["service_date"] else "\\N",
        ]
        buf.write("\t".join(row) + "\n")
    buf.seek(0)

    with conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE _pred_staging ("
            "trip_id text, stop_sequence int, "
            "predicted_time timestamptz, delay_seconds int, "
            "vehicle_id text, poll_timestamp timestamptz, "
            "service_date date"
            ") ON COMMIT DROP"
        )
        cur.copy_from(buf, "_pred_staging", sep="\t", null="\\N", columns=cols)
        cur.execute(
            "INSERT INTO real_time_observations "
            "    (trip_id, stop_sequence, predicted_time, delay_seconds, "
            "     vehicle_id, poll_timestamp, service_date) "
            "SELECT trip_id, stop_sequence, predicted_time, delay_seconds, "
            "       vehicle_id, poll_timestamp, service_date "
            "FROM _pred_staging "
            "ON CONFLICT (trip_id, stop_sequence, service_date) "
            "DO UPDATE SET "
            "    predicted_time = EXCLUDED.predicted_time, "
            "    delay_seconds  = EXCLUDED.delay_seconds, "
            "    vehicle_id     = EXCLUDED.vehicle_id, "
            "    poll_timestamp = EXCLUDED.poll_timestamp"
        )
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
