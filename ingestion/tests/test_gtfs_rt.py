from datetime import date, datetime, timezone

from poller.gtfs_rt import (
    scheduled_to_ts,
    _parse_time_str,
    extract_observations,
    infer_service_date,
    classify,
)


class _FakeStatic:
    """Minimal stand-in for gtfs_static.StaticDB — point stop_time lookups only."""

    def __init__(self, cache):
        self._cache = cache

    def stop_time(self, trip_id, stop_sequence):
        return self._cache.get((trip_id, stop_sequence))


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
        ("t1", 1): {"arrival_time": "17:40:00", "stop_id": "A"},
        ("t1", 2): {"arrival_time": "17:42:00", "stop_id": "B"},
    }

    obs = extract_observations(feed, _FakeStatic(cache))
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
        ("t-midnight", 1): {"arrival_time": "24:11:43", "stop_id": "A"},
    }

    obs = extract_observations(feed, _FakeStatic(cache))

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

    obs = extract_observations(feed, _FakeStatic({}))
    assert len(obs) == 0


def test_extract_observations_missing_cache():
    """Trip with no stop_times in cache → no observations"""
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.timestamp = 1783374000

    e = feed.entity.add()
    e.trip_update.trip.trip_id = "unknown_trip"

    obs = extract_observations(feed, _FakeStatic({}))
    assert len(obs) == 0


def test_scheduled_to_ts_fall_back():
    """Fall-back Nov 1 — 1:30 AM repeats twice (EDT then EST).

    scheduled_to_ts constructs the datetime with fold=0 (default),
    which is the first occurrence: 1:30 AM EDT (UTC-4) = 05:30 UTC.
    """
    ts = scheduled_to_ts("01:30:00", date(2026, 11, 1))
    utc_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert utc_dt.day == 1
    assert utc_dt.hour == 5
    assert utc_dt.minute == 30


def test_infer_service_date_large_delay():
    """Bus 2 hours late — scheduled 23:30 on July 22, predicted 01:30 July 23.

    infer_service_date should still pick July 22 as the service date
    because the scheduled timestamp for July 22 is closer than July 23.
    """
    scheduled_ts = scheduled_to_ts("23:30:00", date(2026, 7, 22))
    predicted_ts = scheduled_ts + 2 * 3600

    result = infer_service_date("23:30:00", predicted_ts)
    assert result == date(2026, 7, 22)


def test_infer_service_date_dst_boundary():
    """At DST spring-forward boundary, infer_service_date still picks correctly.

    March 8 2026: clocks spring forward at 2:00 AM.
    A bus scheduled at 01:30 AM that arrives at 03:30 AM (after the gap)
    should still be attributed to March 8.
    """
    scheduled_ts = scheduled_to_ts("01:30:00", date(2026, 3, 8))
    predicted_ts = scheduled_ts + 2 * 3600

    result = infer_service_date("01:30:00", predicted_ts)
    assert result == date(2026, 3, 8)


def test_extract_observations_midnight_crossing_stops():
    """Stop 1 at 23:59, stop 2 at 00:01 — both should get the same service_date."""
    from google.transit import gtfs_realtime_pb2

    ts1 = scheduled_to_ts("23:59:00", date(2026, 7, 22))
    ts2 = scheduled_to_ts("24:01:00", date(2026, 7, 22))

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.timestamp = ts2

    e = feed.entity.add()
    e.trip_update.trip.trip_id = "t-cross"

    stu1 = e.trip_update.stop_time_update.add()
    stu1.stop_sequence = 1
    stu1.arrival.time = ts1

    stu2 = e.trip_update.stop_time_update.add()
    stu2.stop_sequence = 2
    stu2.arrival.time = ts2

    cache = {
        ("t-cross", 1): {"arrival_time": "23:59:00", "stop_id": "A"},
        ("t-cross", 2): {"arrival_time": "24:01:00", "stop_id": "B"},
    }

    obs = extract_observations(feed, _FakeStatic(cache))
    assert len(obs) == 2
    assert obs[0]["service_date"] == date(2026, 7, 22)
    assert obs[1]["service_date"] == date(2026, 7, 22)


def test_extract_observations_exact_midnight():
    """24:00:00 scheduled — prediction exactly at midnight boundary."""
    from google.transit import gtfs_realtime_pb2

    predicted_ts = scheduled_to_ts("24:00:00", date(2026, 7, 22))

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.timestamp = predicted_ts

    e = feed.entity.add()
    e.trip_update.trip.trip_id = "t-midnight-exact"

    stu = e.trip_update.stop_time_update.add()
    stu.stop_sequence = 1
    stu.arrival.time = predicted_ts

    cache = {
        ("t-midnight-exact", 1): {"arrival_time": "24:00:00", "stop_id": "A"},
    }

    obs = extract_observations(feed, _FakeStatic(cache))
    assert len(obs) == 1
    assert obs[0]["service_date"] == date(2026, 7, 22)
    assert obs[0]["delay_seconds"] == 0


def test_classify_early():
    assert classify(-61) == "early"
    assert classify(-3600) == "early"


def test_classify_on_time():
    assert classify(-60) == "on_time"
    assert classify(0) == "on_time"
    assert classify(300) == "on_time"


def test_classify_late():
    assert classify(301) == "late"
    assert classify(3600) == "late"
