import json
from datetime import date

from poller.rollup import build_rollup, write_current
from poller.state import ObservationsDB

STATIC = {
    "routes": {
        "bus42": {"route_name": "42", "route_type": 3},
        "trolley10": {"route_name": "10", "route_type": 0},
    },
    "stops": {
        "S1": {"stop_name": "Front & Chestnut", "stop_lat": 39.952, "stop_lon": -75.165},
        "S2": {"stop_name": "10th & Main", "stop_lat": 39.960, "stop_lon": -75.170},
    },
}


def _make_db(tmp_path):
    db = ObservationsDB(tmp_path / "obs.db")
    for trip, route, stop, delay, cat in [
        ("t1", "bus42", "S1", 60, "on_time"),
        ("t2", "bus42", "S2", -120, "early"),
        ("t3", "trolley10", "S2", 400, "late"),
    ]:
        db.upsert(
            [
                {
                    "trip_id": trip,
                    "stop_sequence": 1,
                    "service_date": date(2026, 8, 19),
                    "route_id": route,
                    "stop_id": stop,
                    "delay_seconds": delay,
                    "category": cat,
                    "vehicle_id": None,
                    "predicted_time": None,
                    "poll_timestamp": None,
                }
            ]
        )
    return db


class TestBuildRollup:
    def test_shape(self, tmp_path):
        db = _make_db(tmp_path)
        try:
            rollup = build_rollup(db, "2026-08-19", STATIC)
            assert set(rollup.keys()) == {"service_date", "updated_at", "routes", "stops"}
            assert rollup["service_date"] == "2026-08-19"
        finally:
            db.close()

    def test_route_metadata_merged(self, tmp_path):
        db = _make_db(tmp_path)
        try:
            rollup = build_rollup(db, "2026-08-19", STATIC)
            r = rollup["routes"]["bus42"]
            assert r["route_name"] == "42"
            assert r["route_type"] == 3
            assert r["total_observations"] == 2
            assert r["on_time_count"] == 1
            assert r["early_count"] == 1
            assert r["late_count"] == 0
            assert r["delay_sum"] == -60
        finally:
            db.close()

    def test_stop_metadata_merged(self, tmp_path):
        db = _make_db(tmp_path)
        try:
            rollup = build_rollup(db, "2026-08-19", STATIC)
            s = rollup["stops"]["S2"]
            assert s["stop_name"] == "10th & Main"
            assert s["stop_lat"] == 39.960
            assert s["total_observations"] == 2
            assert s["late_count"] == 1
            assert "S3" not in rollup["stops"]
        finally:
            db.close()

    def test_accepts_date_object(self, tmp_path):
        db = _make_db(tmp_path)
        try:
            rollup = build_rollup(db, date(2026, 8, 19), STATIC)
            assert rollup["service_date"] == "2026-08-19"
        finally:
            db.close()

    def test_empty_store(self, tmp_path):
        db = ObservationsDB(tmp_path / "obs.db")
        try:
            rollup = build_rollup(db, "2026-08-19", STATIC)
            assert rollup["routes"] == {}
            assert rollup["stops"] == {}
        finally:
            db.close()


class TestWriteCurrent:
    def test_writes_atomic_json(self, tmp_path):
        db = _make_db(tmp_path)
        try:
            rollup = build_rollup(db, "2026-08-19", STATIC)
            out = tmp_path / "rollups"
            write_current(rollup, str(out))
            loaded = json.loads((out / "current.json").read_text(encoding="utf-8"))
            assert loaded["service_date"] == "2026-08-19"
            assert loaded["stops"]["S2"]["total_observations"] == 2
            assert not (out / "current.json.tmp").exists()
        finally:
            db.close()