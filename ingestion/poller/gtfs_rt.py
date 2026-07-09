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

        # infer service date from the median predicted timestamp
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


def load_stop_times(session, trip_ids: set[str]) -> dict:
    from sqlalchemy import text

    rows = session.execute(
        text(
            """
        SELECT trip_id, stop_sequence, arrival_time, stop_id
        FROM stop_times
        WHERE trip_id = ANY(:trip_ids)
        ORDER BY trip_id, stop_sequence
        """
        ),
        {"trip_ids": list(trip_ids)},
    ).fetchall()

    cache: dict[str, dict[int, dict]] = {}
    for row in rows:
        tid = row[0]
        seq = row[1]
        if tid not in cache:
            cache[tid] = {}
        cache[tid][seq] = {
            "arrival_time": row[2],
            "stop_id": row[3],
        }
    return cache


def build_aggregations(session):
    from sqlalchemy import text

    now = datetime.now(timezone.utc)
    today = now.date()

    # Use the latest prediction per (trip, stop, day) so each scheduled stop
    # counts at most once — the prediction closest to when the bus actually arrives.
    cte = """
        WITH latest AS (
            SELECT DISTINCT ON (trip_id, stop_id, stop_sequence, poll_timestamp::date)
                route_id,
                delay_seconds,
                poll_timestamp
            FROM arrival_records
            WHERE poll_timestamp >= DATE(:today)
            ORDER BY trip_id, stop_id, stop_sequence, poll_timestamp::date, poll_timestamp DESC
        )
    """

    # --- daily aggregation ---
    session.execute(
        text(
            cte
            + """
        INSERT INTO daily_route_metrics
            (route_id, date, total_observations, early_count, on_time_count, late_count,
             on_time_percentage, avg_delay_seconds)
        SELECT
            route_id,
            DATE(poll_timestamp) AS date,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE delay_seconds < -60) AS early,
            COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300) AS on_time,
            COUNT(*) FILTER (WHERE delay_seconds > 300) AS late,
            ROUND(
                100.0 * COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300)
                / NULLIF(COUNT(*), 0), 1
            ),
            ROUND(AVG(delay_seconds)::numeric, 1)
        FROM latest
        GROUP BY route_id, DATE(poll_timestamp)
        ON CONFLICT (route_id, date)
        DO UPDATE SET
            total_observations = EXCLUDED.total_observations,
            early_count       = EXCLUDED.early_count,
            on_time_count     = EXCLUDED.on_time_count,
            late_count        = EXCLUDED.late_count,
            on_time_percentage = EXCLUDED.on_time_percentage,
            avg_delay_seconds  = EXCLUDED.avg_delay_seconds
        """
        ),
        {"today": today.isoformat()},
    )

    # --- hourly aggregation ---
    session.execute(
        text(
            cte
            + """
        INSERT INTO hourly_route_metrics
            (route_id, date, hour, total_observations, early_count, on_time_count,
             late_count, on_time_percentage, avg_delay_seconds)
        SELECT
            route_id,
            DATE(poll_timestamp) AS date,
            EXTRACT(HOUR FROM poll_timestamp)::int AS hour,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE delay_seconds < -60) AS early,
            COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300) AS on_time,
            COUNT(*) FILTER (WHERE delay_seconds > 300) AS late,
            ROUND(
                100.0 * COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300)
                / NULLIF(COUNT(*), 0), 1
            ),
            ROUND(AVG(delay_seconds)::numeric, 1)
        FROM latest
        GROUP BY route_id, DATE(poll_timestamp), EXTRACT(HOUR FROM poll_timestamp)
        ON CONFLICT (route_id, date, hour)
        DO UPDATE SET
            total_observations = EXCLUDED.total_observations,
            early_count       = EXCLUDED.early_count,
            on_time_count     = EXCLUDED.on_time_count,
            late_count        = EXCLUDED.late_count,
            on_time_percentage = EXCLUDED.on_time_percentage,
            avg_delay_seconds  = EXCLUDED.avg_delay_seconds
        """
        ),
        {"today": today.isoformat()},
    )

    # --- latest snapshot ---
    session.execute(
        text(
            """
        INSERT INTO latest_snapshot
            (route_id, route_name, route_type, total_observations,
             early_count, on_time_count, late_count,
             on_time_percentage, avg_delay_seconds, updated_at)
        SELECT
            m.route_id,
            r.route_short_name,
            r.route_type,
            m.total_observations,
            m.early_count,
            m.on_time_count,
            m.late_count,
            m.on_time_percentage,
            m.avg_delay_seconds,
            :now AS updated_at
        FROM daily_route_metrics m
        LEFT JOIN routes r ON r.route_id = m.route_id
        WHERE m.date = :today
        ON CONFLICT (route_id)
        DO UPDATE SET
            route_name         = EXCLUDED.route_name,
            route_type         = EXCLUDED.route_type,
            total_observations = EXCLUDED.total_observations,
            early_count       = EXCLUDED.early_count,
            on_time_count     = EXCLUDED.on_time_count,
            late_count        = EXCLUDED.late_count,
            on_time_percentage = EXCLUDED.on_time_percentage,
            avg_delay_seconds  = EXCLUDED.avg_delay_seconds,
            updated_at        = EXCLUDED.updated_at
        """
        ),
        {"today": today.isoformat(), "now": now},
    )

    session.commit()
