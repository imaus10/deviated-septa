"""Parquet archives tests — pyarrow roundtrip + schema + valid-window rows.

Writes go to tmp dirs only; nothing hits the network or the repo state dir.
"""

import pathlib

import pyarrow.fs as pyarrow_fs
import pyarrow.parquet as pq
import pytest

import poller.archives as archives


def _obs_rows():
    return [
        ("t100", 5, "2026-08-27", "62", "1234", 45, "on_time", "v1", 1724800000, 1724800100),
        ("t101", 2, "2026-08-27", "78", "5678", -80, "early", None, None, 1724800100),
    ]


def _many_obs_rows(count, service_date="2026-08-27"):
    """`count` distinct-ish observation row tuples (cheap to build)."""
    base = (1, service_date, "62", "1234", 45, "on_time", "v1", 1724800000, 1724800100)
    return [(f"t{i}", *base) for i in range(count)]


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


class TestStreaming:
    N = 2 * archives.ROW_GROUP_SIZE + 500  # 400,500 rows -> 3 row groups

    def _write_many(self, tmp_path):
        dirname = tmp_path / "observations"
        return archives.write_observations(_many_obs_rows(self.N), dirname)

    def test_written_archive_is_split_into_row_groups(self, tmp_path):
        p = self._write_many(tmp_path)
        m = pq.ParquetFile(p).metadata
        assert m.num_row_groups == 3
        assert [m.row_group(i).num_rows for i in range(3)] == [archives.ROW_GROUP_SIZE] * 2 + [500]

    def test_row_groups_use_zstd(self, tmp_path):
        p = self._write_many(tmp_path)
        rg = pq.ParquetFile(p).metadata.row_group(0)
        assert rg.num_columns == 10
        assert all(rg.column(i).compression == "ZSTD" for i in range(rg.num_columns))

    def test_stream_observation_yields_batches_summing_to_all_rows(self, tmp_path):
        p = self._write_many(tmp_path)
        batches = list(archives.stream_observation(p))
        assert sum(b.num_rows for b in batches) == self.N
        assert all(b.num_rows <= archives.STREAM_BATCH_SIZE for b in batches)

    def test_stream_reads_via_filesystem(self, tmp_path):
        p = archives.write_observations(_obs_rows(), tmp_path / "observations")
        batches = list(
            archives.stream_observation(p, filesystem=pyarrow_fs.LocalFileSystem())
        )
        assert sum(b.num_rows for b in batches) == 2


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


class TestBuildRegistriesConsolidation:
    """Merge semantics: present open-ended, dropped closed, existing carried."""

    def _data(self):
        return {
            "routes": {
                "42": {"route_name": "42", "route_type": 3},
                "10": {"route_name": "10", "route_type": 0},
                "62": {"route_name": "62", "route_type": 3},
            },
            "stops": {
                "A": {"stop_name": "A", "stop_lat": 39.95, "stop_lon": -75.16},
            },
            "calendar": {
                "wk": {"start_date": "20260823", "end_date": "20260920"},
            },
        }

    def test_active_open_ended_with_calendar_valid_from(self):
        routes, _ = archives.build_registries(self._data(), active_routes={"42", "10"})
        assert routes["42"] == {"name": "42", "route_type": 3,
                                "valid_from": "2026-08-23", "valid_to": None}
        assert "62" not in routes  # dropped, no window known

    def test_dropped_closed_with_observation_window(self):
        windows = {"62": ("2026-07-21", "2026-08-10")}
        routes, _ = archives.build_registries(self._data(), active_routes={"42", "10"},
                                              route_windows=windows)
        assert routes["62"] == {"name": "62", "route_type": 3,
                                "valid_from": "2026-07-21", "valid_to": "2026-08-10"}

    def test_active_route_never_closed_even_with_window(self):
        windows = {"42": ("2026-07-21", "2026-08-10")}
        routes, _ = archives.build_registries(self._data(), active_routes={"42", "10"},
                                              route_windows=windows)
        assert routes["42"]["valid_to"] is None

    def test_preserves_existing_valid_from_for_active(self):
        existing = {"42": {"id": "42", "name": "42", "route_type": 3,
                           "valid_from": "2026-07-21", "valid_to": None}}
        routes, _ = archives.build_registries(self._data(), active_routes={"42", "10"},
                                              existing_routes=existing)
        assert routes["42"]["valid_from"] == "2026-07-21"

    def test_window_valid_from_falls_back_to_existing(self):
        existing = {"62": {"id": "62", "name": "62", "route_type": 3,
                           "valid_from": "2026-07-21", "valid_to": None}}
        routes, _ = archives.build_registries(self._data(), active_routes={"42", "10"},
                                              existing_routes=existing,
                                              route_windows={"62": (None, "2026-08-10")})
        assert routes["62"]["valid_from"] == "2026-07-21"
        assert routes["62"]["valid_to"] == "2026-08-10"

    def test_carries_existing_ids_not_in_feed(self):
        existing = {"99": {"id": "99", "name": "99", "route_type": 3,
                           "valid_from": "2026-07-21", "valid_to": "2026-08-01"}}
        routes, _ = archives.build_registries(self._data(), active_routes={"42", "10"},
                                              existing_routes=existing)
        assert routes["99"] == existing["99"]

    def test_closed_row_never_reopened_when_inactive(self):
        existing = {"62": {"id": "62", "name": "62", "route_type": 3,
                           "valid_from": "2026-07-21", "valid_to": "2026-08-10"}}
        routes, _ = archives.build_registries(self._data(), active_routes={"42", "10"},
                                              existing_routes=existing)
        assert routes["62"]["valid_to"] == "2026-08-10"

    def test_stops_preserve_existing_valid_from(self):
        existing = {"A": {"id": "A", "name": "A", "stop_lat": 39.95, "stop_lon": -75.16,
                          "valid_from": "2026-07-21", "valid_to": None}}
        _, stops = archives.build_registries(self._data(), existing_stops=existing)
        assert stops["A"]["valid_from"] == "2026-07-21"
        assert stops["A"]["valid_to"] is None


class TestReadRegistry:
    def test_roundtrip(self, tmp_path):
        routes = {
            "42": {"name": "42", "route_type": 3, "valid_from": "2026-08-23", "valid_to": None},
            "62": {"name": "62", "route_type": 3, "valid_from": "2026-07-21", "valid_to": "2026-08-10"},
        }
        p = archives.write_routes_registry(routes, tmp_path)
        back = archives.read_registry(p)
        assert back["42"]["valid_to"] is None
        assert back["42"]["valid_from"] == "2026-08-23"
        assert back["62"]["valid_to"] == "2026-08-10"


class TestAtomicWrite:
    def test_no_tmp_left_over(self, tmp_path):
        archives.write_observations(_obs_rows(), tmp_path / "observations")
        leftovers = [f.name for f in (tmp_path / "observations").iterdir() if f.suffix == ".tmp"]
        assert leftovers == []

    def test_read_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            archives.read_observation(pathlib.Path(tmp_path) / "nope.parquet")
