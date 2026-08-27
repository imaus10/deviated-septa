import logging
from datetime import datetime, date, timedelta, timezone

import httpx
from google.transit import gtfs_realtime_pb2

from poller.constants import (
    EARLY_TOLERANCE_SECONDS,
    EASTERN,
    LATE_TOLERANCE_SECONDS,
)

log = logging.getLogger(__name__)

BUS_TRIP_UPDATES = "https://www3.septa.org/gtfsrt/septa-pa-us/Trip/rtTripUpdates.pb"
BUS_VEHICLE_POSITIONS = (
    "https://www3.septa.org/gtfsrt/septa-pa-us/Vehicle/rtVehiclePosition.pb"
)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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


def infer_service_date(arrival_time: str, predicted_ts: int) -> date:
    predicted_date = datetime.fromtimestamp(int(predicted_ts), tz=EASTERN).date()
    candidates = [
        predicted_date + timedelta(days=offset)
        for offset in (-1, 0, 1)
    ]
    return min(
        candidates,
        key=lambda candidate: abs(
            int(predicted_ts) - scheduled_to_ts(arrival_time, candidate)
        ),
    )


def fetch_protobuf(url: str) -> bytes:
    resp = httpx.get(url, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    return resp.content


def parse_trip_updates(raw: bytes) -> gtfs_realtime_pb2.FeedMessage:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(raw)
    return feed


def extract_observations(feed, stop_times_data: dict) -> list[dict]:
    """Extract observations from GTFS-RT feed.

    Args:
        stop_times_data: {(trip_id, stop_seq): {"arrival_time": str, "stop_id": str}}
            as returned by gtfs_static.parse_zip()
    """
    observations = []

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        tu = entity.trip_update
        trip_id = tu.trip.trip_id

        if tu.trip.schedule_relationship == gtfs_realtime_pb2.TripDescriptor.CANCELED:
            continue

        vehicle_id = tu.vehicle.id if tu.vehicle.id else None

        for stu in tu.stop_time_update:
            if not (stu.HasField("arrival") and stu.arrival.time > 0):
                continue

            stop_seq = stu.stop_sequence
            predicted_ts = stu.arrival.time

            scheduled_row = stop_times_data.get((trip_id, stop_seq))
            if scheduled_row is None:
                continue

            arrival = scheduled_row.get("arrival_time")
            if not arrival:
                log.warning("skip trip=%s seq=%s: empty arrival_time", trip_id, stop_seq)
                continue

            service_date = infer_service_date(arrival, int(predicted_ts))
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


def classify(delay: int) -> str:
    """Classify delay into on-time category."""
    if delay < EARLY_TOLERANCE_SECONDS:
        return "early"
    elif delay > LATE_TOLERANCE_SECONDS:
        return "late"
    return "on_time"