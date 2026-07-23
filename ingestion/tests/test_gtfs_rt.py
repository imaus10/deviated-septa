from datetime import date, datetime, timezone

from poller.gtfs_rt import (
    scheduled_to_ts,
    _parse_time_str,
    extract_observations,
    infer_service_date,
)


def test_parse_time_str_normal():
    assert _parse_time_str("12:30:45") == (0, 12, 30, 45)


def test_parse_time_str_midnight():
    assert _parse_time_str("25:15:00") == (1, 1, 15, 0)


def test_parse_time_str_zero():
    assert _parse_time_str("00:00:00") == (0, 0, 0, 0)


def test_scheduled_to_ts_edt():
    """17:40 EDT (July) = 21:40 UTC"""
    ts = scheduled_to_ts("17:40:00", date(2026, 7, 6))
    utc_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert utc_dt.hour == 21
    assert utc_dt.minute == 40


def test_scheduled_to_ts_est():
    """17:40 EST (December) = 22:40 UTC"""
    ts = scheduled_to_ts("17:40:00", date(2026, 12, 6))
    utc_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert utc_dt.hour == 22
    assert utc_dt.minute == 40


def test_scheduled_to_ts_midnight():
    """25:40 = next day 1:40 AM EDT = 5:40 UTC"""
    ts = scheduled_to_ts("25:40:00", date(2026, 7, 6))
    utc_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert utc_dt.day == 7
    assert utc_dt.hour == 5
    assert utc_dt.minute == 40


def test_scheduled_to_ts_dst_spring_forward():
    """Spring forward gap — 2:30 AM doesn't exist, zoneinfo uses EST (UTC-5)

    On March 8, 2:00 AM EST springs to 3:00 AM EDT, so 2:30 AM is
    interpreted as pre-transition EST = 7:30 UTC.
    """
    ts = scheduled_to_ts("02:30:00", date(2026, 3, 8))
    utc_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert utc_dt.hour == 7
    assert utc_dt.minute == 30


def test_infer_service_date_for_after_midnight_gtfs_time():
    """24:11 on July 22 is 00:11 on July 23, not service date July 23."""
    predicted_ts = scheduled_to_ts("24:11:43", date(2026, 7, 22)) + 60

    assert infer_service_date("24:11:43", predicted_ts) == date(2026, 7, 22)


def test_infer_service_date_for_late_before_midnight_stop():
    """A 23:59 scheduled stop arriving after midnight belongs to prior service day."""
    predicted_ts = scheduled_to_ts("23:59:00", date(2026, 7, 22)) + 3 * 60

    assert infer_service_date("23:59:00", predicted_ts) == date(2026, 7, 22)


def test_infer_service_date_for_early_after_midnight_stop():
    """An after-midnight service-day stop predicted before midnight can be early."""
    predicted_ts = scheduled_to_ts("00:02:00", date(2026, 7, 23)) - 3 * 60

    assert infer_service_date("00:02:00", predicted_ts) == date(2026, 7, 23)


def test_extract_observations_basic():
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.timestamp = 1783374000  # 2026-07-06 17:40 EDT

    e = feed.entity.add()
    e.trip_update.trip.trip_id = "t1"
    e.trip_update.trip.route_id = "42"
    e.trip_update.trip.direction_id = 0

    stu = e.trip_update.stop_time_update.add()
    stu.stop_sequence = 1
    stu.stop_id = "A"
    stu.arrival.time = 1783374060  # 17:41 EDT (1 min late → +60s)

    stu = e.trip_update.stop_time_update.add()
    stu.stop_sequence = 2
    stu.stop_id = "B"
    stu.arrival.time = 1783374120  # 17:42 EDT (on time)

    cache = {
        "t1": {
            1: {"arrival_time": "17:40:00", "stop_id": "A"},
            2: {"arrival_time": "17:42:00", "stop_id": "B"},
        }
    }

    obs = extract_observations(feed, cache)
    assert len(obs) == 2
    assert obs[0]["trip_id"] == "t1"
    assert obs[0]["stop_sequence"] == 1
    assert obs[0]["delay_seconds"] == 60  # predicted 17:41 vs scheduled 17:40
    assert abs(obs[1]["delay_seconds"]) <= 1  # predicted 17:42 vs scheduled 17:42
    assert obs[0]["service_date"] == date(2026, 7, 6)


def test_extract_observations_after_midnight_gtfs_time():
    from google.transit import gtfs_realtime_pb2

    predicted_ts = scheduled_to_ts("24:11:43", date(2026, 7, 22)) + 60

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.timestamp = predicted_ts

    e = feed.entity.add()
    e.trip_update.trip.trip_id = "t-midnight"

    stu = e.trip_update.stop_time_update.add()
    stu.stop_sequence = 1
    stu.arrival.time = predicted_ts

    cache = {
        "t-midnight": {
            1: {"arrival_time": "24:11:43", "stop_id": "A"},
        }
    }

    obs = extract_observations(feed, cache)

    assert len(obs) == 1
    assert obs[0]["service_date"] == date(2026, 7, 22)
    assert obs[0]["delay_seconds"] == 60


def test_extract_observations_cancelled():
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.timestamp = 1783374000

    e = feed.entity.add()
    e.trip_update.trip.trip_id = "cancelled"
    e.trip_update.trip.route_id = "99"
    e.trip_update.trip.schedule_relationship = (
        gtfs_realtime_pb2.TripDescriptor.CANCELED
    )

    obs = extract_observations(feed, {})
    assert len(obs) == 0


def test_extract_observations_missing_cache():
    """Trip with no stop_times in cache → no observations"""
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.timestamp = 1783374000

    e = feed.entity.add()
    e.trip_update.trip.trip_id = "unknown_trip"

    obs = extract_observations(feed, {})
    assert len(obs) == 0
