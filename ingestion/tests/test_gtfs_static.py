import io
import csv
import zipfile
import pytest

from poller.gtfs_static import parse_zip, load_local, get_stored_freshness, _save_zip


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


# --- Route parsing ---

class TestParseRoutes:
    def test_includes_bus(self):
        result = parse_zip(make_test_zip())
        assert "bus42" in result["routes"]

    def test_includes_trolley(self):
        result = parse_zip(make_test_zip())
        assert "trolley10" in result["routes"]

    def test_excludes_rail(self):
        result = parse_zip(make_test_zip())
        assert "rail100" not in result["routes"]

    def test_route_fields(self):
        result = parse_zip(make_test_zip())
        r = result["routes"]["bus42"]
        assert r["route_name"] == "42"
        assert r["route_type"] == 3

    def test_no_bus_or_trolley(self):
        routes = [{"route_id": "r1", "route_short_name": "1", "route_long_name": "Rail", "route_type": "1"}]
        result = parse_zip(make_test_zip(routes=routes))
        assert result["routes"] == {}


# --- Trip parsing ---

class TestParseTrips:
    def test_includes_bus_trips(self):
        result = parse_zip(make_test_zip())
        assert "t1" in result["trips"]

    def test_includes_trolley_trips(self):
        result = parse_zip(make_test_zip())
        assert "t2" in result["trips"]

    def test_excludes_rail_trips(self):
        result = parse_zip(make_test_zip())
        assert "t3" not in result["trips"]

    def test_trip_route_id(self):
        result = parse_zip(make_test_zip())
        assert result["trips"]["t1"]["route_id"] == "bus42"

    def test_trip_fields(self):
        result = parse_zip(make_test_zip())
        t = result["trips"]["t1"]
        assert t["direction_id"] == 0
        assert t["trip_headsign"] == "Outbound"


# --- Stop parsing ---

class TestParseStops:
    def test_includes_all_stops(self):
        """Stops are NOT filtered — a stop used by both bus and rail is included."""
        result = parse_zip(make_test_zip())
        assert "S1" in result["stops"]
        assert "S2" in result["stops"]
        assert "S3" in result["stops"]

    def test_stop_fields(self):
        result = parse_zip(make_test_zip())
        s = result["stops"]["S1"]
        assert s["stop_name"] == "Front & Chestnut"
        assert s["stop_lat"] == pytest.approx(39.952)
        assert s["stop_lon"] == pytest.approx(-75.165)


# --- Stop times parsing ---

class TestParseStopTimes:
    def test_includes_bus_stop_times(self):
        result = parse_zip(make_test_zip())
        assert ("t1", 1) in result["stop_times"]

    def test_includes_trolley_stop_times(self):
        result = parse_zip(make_test_zip())
        assert ("t2", 1) in result["stop_times"]

    def test_excludes_rail_stop_times(self):
        result = parse_zip(make_test_zip())
        assert ("t3", 1) not in result["stop_times"]

    def test_stop_time_fields(self):
        result = parse_zip(make_test_zip())
        st = result["stop_times"][("t1", 1)]
        assert st["arrival_time"] == "10:00:00"
        assert st["stop_id"] == "S1"


# --- Calendar parsing ---

class TestParseCalendar:
    def test_includes_all_calendars(self):
        result = parse_zip(make_test_zip())
        assert " weekday" in result["calendar"]

    def test_calendar_fields(self):
        result = parse_zip(make_test_zip())
        c = result["calendar"][" weekday"]
        assert c["monday"] == 1
        assert c["start_date"] == "20260101"


# --- Integration ---

class TestParseZipIntegration:
    def test_full_parse(self):
        data = parse_zip(make_test_zip())
        assert set(data.keys()) == {"routes", "trips", "stops", "stop_times", "calendar"}
        assert len(data["routes"]) == 2
        assert len(data["trips"]) == 2
        assert len(data["stops"]) == 3
        assert len(data["stop_times"]) == 3
        assert len(data["calendar"]) == 1

    def test_invalid_zip(self):
        with pytest.raises(zipfile.BadZipFile):
            parse_zip(b"not a zip")

    def test_missing_inner_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("something.txt", "hello")
        with pytest.raises(KeyError):
            parse_zip(buf.getvalue())

    def test_empty_stop_times(self):
        result = parse_zip(make_test_zip(stop_times=[]))
        assert result["stop_times"] == {}

    def test_empty_trips(self):
        result = parse_zip(make_test_zip(trips=[]))
        assert result["trips"] == {}
        assert result["stop_times"] == {}  # no trips → no stop_times


# --- Local save/load ---

class TestLocalSaveLoad:
    def test_round_trip(self, tmp_path):
        zip_bytes = make_test_zip()
        data_dir = tmp_path / "gtfs-static"

        # Save
        _save_zip(str(data_dir), zip_bytes, "Mon, 18 Aug 2026 14:30:00 GMT")

        # Verify files exist
        assert (data_dir / "freshness.txt").read_text() == "Mon, 18 Aug 2026 14:30:00 GMT"
        assert (data_dir / "latest.zip").stat().st_size > 0

        # Load
        loaded = load_local(str(data_dir))
        original = parse_zip(zip_bytes)
        assert loaded["routes"].keys() == original["routes"].keys()
        assert loaded["stop_times"].keys() == original["stop_times"].keys()

    def test_load_missing_dir(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_local(str(tmp_path / "nonexistent"))

    def test_load_missing_freshness(self, tmp_path):
        data_dir = tmp_path / "gtfs-static"
        data_dir.mkdir()
        (data_dir / "latest.zip").write_bytes(make_test_zip())
        # No freshness.txt → should still load the zip
        loaded = load_local(str(data_dir))
        assert len(loaded["routes"]) == 2


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
