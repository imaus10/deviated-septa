import io
import csv
import zipfile
import pytest

from poller.gtfs_static import (
    StaticDB,
    active_route_ids,
    get_stored_freshness,
    import_to_sqlite,
    load_local_metadata,
    _save_zip,
)


def _csv_bytes(rows: list[dict]) -> bytes:
    """Serialize a list of dicts to CSV bytes (GTFS format)."""
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def make_test_zip(
    routes=None,
    trips=None,
    stops=None,
    stop_times=None,
    calendar=None,
) -> bytes:
    """Build a minimal valid GTFS zip (outer gtfs_public.zip → inner google_bus.zip).

    Defaults provide a small but complete bus/trolley/rail fixture.
    Pass None to use defaults; pass empty list to omit a file entirely.
    """
    if routes is None:
        routes = [
            {"route_id": "bus42", "route_short_name": "42", "route_long_name": "Front-Chestnut", "route_type": "3"},
            {"route_id": "trolley10", "route_short_name": "10", "route_long_name": "10th-Main", "route_type": "0"},
            {"route_id": "rail100", "route_short_name": "100", "route_long_name": "Regional Rail", "route_type": "1"},
        ]
    if trips is None:
        trips = [
            {"trip_id": "t1", "route_id": "bus42", "service_id": " weekday", "direction_id": "0", "trip_headsign": "Outbound"},
            {"trip_id": "t2", "route_id": "trolley10", "service_id": " weekday", "direction_id": "1", "trip_headsign": "Inbound"},
            {"trip_id": "t3", "route_id": "rail100", "service_id": " weekday", "direction_id": "0", "trip_headsign": "North"},
        ]
    if stops is None:
        stops = [
            {"stop_id": "S1", "stop_name": "Front & Chestnut", "stop_lat": "39.952", "stop_lon": "-75.165"},
            {"stop_id": "S2", "stop_name": "10th & Main", "stop_lat": "39.960", "stop_lon": "-75.170"},
            {"stop_id": "S3", "stop_name": "Rail Station", "stop_lat": "39.955", "stop_lon": "-75.160"},
        ]
    if stop_times is None:
        stop_times = [
            {"trip_id": "t1", "stop_sequence": "1", "stop_id": "S1", "arrival_time": "10:00:00", "departure_time": "10:00:00", "pickup_type": "0", "drop_off_type": "0"},
            {"trip_id": "t1", "stop_sequence": "2", "stop_id": "S2", "arrival_time": "10:05:00", "departure_time": "10:05:00", "pickup_type": "0", "drop_off_type": "0"},
            {"trip_id": "t2", "stop_sequence": "1", "stop_id": "S2", "arrival_time": "10:10:00", "departure_time": "10:10:00", "pickup_type": "0", "drop_off_type": "0"},
            {"trip_id": "t3", "stop_sequence": "1", "stop_id": "S3", "arrival_time": "10:20:00", "departure_time": "10:20:00", "pickup_type": "0", "drop_off_type": "0"},
        ]
    if calendar is None:
        calendar = [
            {"service_id": " weekday", "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0", "start_date": "20260101", "end_date": "20261231"},
        ]

    files = {}
    if routes is not None:
        files["routes.txt"] = _csv_bytes(routes)
    if trips is not None:
        files["trips.txt"] = _csv_bytes(trips)
    if stops is not None:
        files["stops.txt"] = _csv_bytes(stops)
    if stop_times is not None:
        files["stop_times.txt"] = _csv_bytes(stop_times)
    if calendar is not None:
        files["calendar.txt"] = _csv_bytes(calendar)

    # Inner zip: google_bus.zip
    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    inner_bytes = inner_buf.getvalue()

    # Outer zip: gtfs_public.zip
    outer_buf = io.BytesIO()
    with zipfile.ZipFile(outer_buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("google_bus.zip", inner_bytes)
    return outer_buf.getvalue()


@pytest.fixture
def static(tmp_path):
    """Build a StaticDB from a fixture zip written to a tmp data dir."""
    data_dir = tmp_path / "gtfs-static"
    data_dir.mkdir()
    (data_dir / "latest.zip").write_bytes(make_test_zip())
    db_path = tmp_path / "static.db"
    import_to_sqlite(str(data_dir), str(db_path))
    s = StaticDB(str(db_path))
    yield s
    s.close()


def _metadata(tmp_path, **kwargs):
    data_dir = tmp_path / "gtfs-static"
    data_dir.mkdir()
    (data_dir / "latest.zip").write_bytes(make_test_zip(**kwargs))
    return load_local_metadata(str(data_dir))


# --- Route parsing (metadata) ---

class TestParseRoutes:
    def test_includes_bus(self, tmp_path):
        assert "bus42" in _metadata(tmp_path)["routes"]

    def test_includes_trolley(self, tmp_path):
        assert "trolley10" in _metadata(tmp_path)["routes"]

    def test_excludes_rail(self, tmp_path):
        assert "rail100" not in _metadata(tmp_path)["routes"]

    def test_route_fields(self, tmp_path):
        r = _metadata(tmp_path)["routes"]["bus42"]
        assert r["route_name"] == "42"
        assert r["route_type"] == 3

    def test_no_bus_or_trolley(self, tmp_path):
        routes = [{"route_id": "r1", "route_short_name": "1", "route_long_name": "Rail", "route_type": "1"}]
        assert _metadata(tmp_path, routes=routes)["routes"] == {}


# --- Stop parsing (metadata) ---

class TestParseStops:
    def test_includes_all_stops(self, tmp_path):
        """Stops are NOT filtered — a stop used by both bus and rail is included."""
        stops = _metadata(tmp_path)["stops"]
        assert "S1" in stops
        assert "S2" in stops
        assert "S3" in stops

    def test_stop_fields(self, tmp_path):
        s = _metadata(tmp_path)["stops"]["S1"]
        assert s["stop_name"] == "Front & Chestnut"
        assert s["stop_lat"] == pytest.approx(39.952)
        assert s["stop_lon"] == pytest.approx(-75.165)


# --- Calendar parsing (metadata) ---

class TestParseCalendar:
    def test_includes_all_calendars(self, tmp_path):
        assert " weekday" in _metadata(tmp_path)["calendar"]

    def test_calendar_fields(self, tmp_path):
        c = _metadata(tmp_path)["calendar"][" weekday"]
        assert c["monday"] == 1
        assert c["start_date"] == "20260101"


# --- StaticDB: trips + stop_times lookups ---

class TestStaticDB:
    def test_stop_time_lookup(self, static):
        assert static.stop_time("t1", 1) == {"arrival_time": "10:00:00", "stop_id": "S1"}

    def test_stop_time_unknown_returns_none(self, static):
        assert static.stop_time("t1", 99) is None

    def test_route_for_trip(self, static):
        assert static.route_for_trip("t1") == "bus42"
        assert static.route_for_trip("t2") == "trolley10"

    def test_route_for_unknown_returns_none(self, static):
        assert static.route_for_trip("nope") is None

    def test_rail_excluded(self, static):
        assert static.route_for_trip("t3") is None
        assert static.stop_time("t3", 1) is None

    def test_iter_stop_times_scoped_and_ordered(self, static):
        rows = list(static.iter_stop_times())
        assert [r[0] for r in rows] == ["t1", "t1", "t2"]
        assert [r[1] for r in rows] == [1, 2, 1]
        # (trip_id, stop_sequence, arrival_time, stop_id)
        assert rows[0] == ("t1", 1, "10:00:00", "S1")
        assert rows[2] == ("t2", 1, "10:10:00", "S2")

    def test_iter_trips_scoped(self, static):
        trips = dict(static.iter_trips())
        assert trips == {"t1": "bus42", "t2": "trolley10"}

    def test_empty_stop_times(self, tmp_path):
        data_dir = tmp_path / "gtfs-static"
        data_dir.mkdir()
        (data_dir / "latest.zip").write_bytes(make_test_zip(stop_times=[]))
        db_path = tmp_path / "static.db"
        import_to_sqlite(str(data_dir), str(db_path))
        s = StaticDB(str(db_path))
        try:
            assert s.stop_time("t1", 1) is None
            assert list(s.iter_stop_times()) == []
            assert s.route_for_trip("t1") == "bus42"  # trips still imported
        finally:
            s.close()

    def test_empty_trips(self, tmp_path):
        data_dir = tmp_path / "gtfs-static"
        data_dir.mkdir()
        (data_dir / "latest.zip").write_bytes(make_test_zip(trips=[]))
        db_path = tmp_path / "static.db"
        import_to_sqlite(str(data_dir), str(db_path))
        s = StaticDB(str(db_path))
        try:
            assert list(s.iter_trips()) == []
            assert list(s.iter_stop_times()) == []  # no trips → no stop_times
        finally:
            s.close()

    def test_reimport_is_idempotent(self, tmp_path):
        data_dir = tmp_path / "gtfs-static"
        data_dir.mkdir()
        (data_dir / "latest.zip").write_bytes(make_test_zip())
        db_path = tmp_path / "static.db"
        import_to_sqlite(str(data_dir), str(db_path))
        import_to_sqlite(str(data_dir), str(db_path))  # rebuild over existing
        s = StaticDB(str(db_path))
        try:
            assert dict(s.iter_trips()) == {"t1": "bus42", "t2": "trolley10"}
        finally:
            s.close()

    def test_missing_zip_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            import_to_sqlite(str(tmp_path / "nonexistent"), str(tmp_path / "s.db"))


# --- Active route ids (for dropped-route registry windows) ---

class TestActiveRouteIds:
    def test_returns_scoped_trip_route_ids(self, tmp_path):
        data_dir = tmp_path / "gtfs-static"
        data_dir.mkdir()
        (data_dir / "latest.zip").write_bytes(make_test_zip())
        assert active_route_ids(str(data_dir)) == {"bus42", "trolley10"}


# --- Local save/load ---

class TestLocalSaveLoad:
    def test_round_trip(self, tmp_path):
        zip_bytes = make_test_zip()
        data_dir = tmp_path / "gtfs-static"
        _save_zip(str(data_dir), zip_bytes, "Mon, 18 Aug 2026 14:30:00 GMT")

        assert (data_dir / "freshness.txt").read_text() == "Mon, 18 Aug 2026 14:30:00 GMT"
        assert (data_dir / "latest.zip").stat().st_size > 0

        metadata = load_local_metadata(str(data_dir))
        assert set(metadata.keys()) == {"routes", "stops", "calendar"}
        assert len(metadata["routes"]) == 2
        assert len(metadata["stops"]) == 3
        assert len(metadata["calendar"]) == 1

    def test_load_missing_dir(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_local_metadata(str(tmp_path / "nonexistent"))

    def test_load_missing_freshness(self, tmp_path):
        data_dir = tmp_path / "gtfs-static"
        data_dir.mkdir()
        (data_dir / "latest.zip").write_bytes(make_test_zip())
        # No freshness.txt → should still load the zip
        assert len(load_local_metadata(str(data_dir))["routes"]) == 2


# --- Freshness ---

class TestFreshness:
    def test_freshness_matches(self, tmp_path):
        data_dir = tmp_path / "gtfs-static"
        _save_zip(str(data_dir), make_test_zip(), "Mon, 18 Aug 2026 14:30:00 GMT")
        assert get_stored_freshness(str(data_dir)) == "Mon, 18 Aug 2026 14:30:00 GMT"

    def test_freshness_missing(self, tmp_path):
        data_dir = tmp_path / "gtfs-static"
        data_dir.mkdir()
        assert get_stored_freshness(str(data_dir)) is None