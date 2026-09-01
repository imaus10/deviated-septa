"""Parquet archives tests — pyarrow roundtrip + schema + valid-window rows.

Writes go to tmp dirs only; nothing hits the network or the repo state dir.
"""

import pathlib

import pytest

import poller.archives as archives


def _obs_rows():
    return [
        ("t100", 5, "2026-08-27", "62", "1234", 45, "on_time", "v1", 1724800000, 1724800100),
        ("t101", 2, "2026-08-27", "78", "5678", -80, "early", None, None, 1724800100),
    ]


class TestWriteObservations:
    def test_roundtrip_preserves_values(self, tmp_path):
        p = archives.write_observations(_obs_rows(), tmp_path / "observations")
        assert p.name == "2026-08-27.parquet"
        t = archives.read_observation(p)
        assert t.num_rows == 2
        # column order / names fixed
        assert t.column_names == list(archives.OBSERVATION_COLUMNS)
        # row 0 values
        assert (t.column("trip_id")[0].as_py(), t.column("stop_sequence")[0].as_py(),
                t.column("delay_seconds")[0].as_py(), t.column("category")[0].as_py()) == \
            ("t100", 5, 45, "on_time")
        assert t.column("predicted_time")[0].as_py() == 1724800000

    def test_nullable_columns_are_null(self, tmp_path):
        p = archives.write_observations(_obs_rows(), tmp_path / "observations")
        t = archives.read_observation(p)
        assert t.column("vehicle_id")[1].as_py() is None
        assert t.column("predicted_time")[1].as_py() is None
        assert t.column("poll_timestamp")[1].as_py() == 1724800100

    def test_empty_rows_raises(self, tmp_path):
        with pytest.raises(ValueError):
            archives.write_observations([], tmp_path / "observations")

    def test_service_date_from_first_row(self, tmp_path):
        rows = _obs_rows()
        rows[1] = ("t101", 2, "2026-08-28", "78", "5678", -80, "early", None, None, 1724800100)
        p = archives.write_observations(rows, tmp_path / "observations")
        assert p.name == "2026-08-27.parquet"


class TestRegistries:
    def test_route_registry_roundtrip(self, tmp_path):
        routes = {
            "62": {"name": "62", "route_type": 3, "valid_from": "2026-07-21", "valid_to": None},
            "D2_BUS": {"name": "D2 Bus", "route_type": 3, "valid_from": "2026-08-08", "valid_to": "2026-08-22"},
        }
        p = archives.write_routes_registry(routes, tmp_path)
        assert p.name == "routes.parquet"
        t = archives.read_observation(p)
        assert t.num_rows == 2
        by_id = {t.column("id")[i].as_py(): {c: t.column(c)[i].as_py() for c in t.column_names}
                 for i in range(t.num_rows)}
        assert by_id["62"]["valid_to"] is None
        assert by_id["62"]["route_type"] == 3
        assert by_id["D2_BUS"]["valid_to"] == "2026-08-22"

    def test_stop_registry_roundtrip(self, tmp_path):
        stops = {
            "1234": {"name": "Front & Chestnut", "stop_lat": 39.9526, "stop_lon": -75.1652,
                     "valid_from": "2026-07-21", "valid_to": None},
        }
        p = archives.write_stops_registry(stops, tmp_path)
        assert p.name == "stops.parquet"
        t = archives.read_observation(p)
        assert abs(t.column("stop_lat")[0].as_py() - 39.9526) < 1e-9

    def test_empty_registry_writes_empty_table(self, tmp_path):
        p = archives.write_routes_registry({}, tmp_path)
        assert archives.read_observation(p).num_rows == 0


class TestBuildRegistries:
    def _data(self):
        return {
            "routes": {
                "42": {"route_name": "42", "route_type": 3},
                "10": {"route_name": "10", "route_type": 0},
            },
            "stops": {
                "A": {"stop_name": "A", "stop_lat": 39.95, "stop_lon": -75.16},
                "B": {"stop_name": "B", "stop_lat": None, "stop_lon": None},
            },
            "calendar": {
                "wk": {"start_date": "20260823", "end_date": "20260920"},
            },
        }

    def test_builds_routes_with_calendar_valid_from(self):
        routes, stops = archives.build_registries(self._data())
        assert routes["42"] == {
            "name": "42", "route_type": 3,
            "valid_from": "2026-08-23", "valid_to": None,
        }

    def test_builds_stops_open_ended(self):
        routes, stops = archives.build_registries(self._data())
        assert stops["A"] == {
            "name": "A", "stop_lat": 39.95, "stop_lon": -75.16,
            "valid_from": "2026-08-23", "valid_to": None,
        }

    def test_calendar_start_none_when_missing(self):
        data = self._data()
        data["calendar"] = {}
        routes, stops = archives.build_registries(data)
        assert routes["42"]["valid_from"] is None


class TestAtomicWrite:
    def test_no_tmp_left_over(self, tmp_path):
        archives.write_observations(_obs_rows(), tmp_path / "observations")
        leftovers = [f.name for f in (tmp_path / "observations").iterdir() if f.suffix == ".tmp"]
        assert leftovers == []

    def test_read_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            archives.read_observation(pathlib.Path(tmp_path) / "nope.parquet")
