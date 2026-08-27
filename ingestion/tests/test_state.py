from datetime import date, datetime, timezone

from poller.state import (
    DEFAULT_STATE,
    ObservationsDB,
    load_state,
    save_state,
)


def _row(
    trip_id="t1",
    stop_seq=1,
    service_date=date(2026, 8, 19),
    route_id="42",
    stop_id="A",
    delay=60,
    category="on_time",
    vehicle_id=None,
):
    return {
        "trip_id": trip_id,
        "stop_sequence": stop_seq,
        "service_date": service_date,
        "route_id": route_id,
        "stop_id": stop_id,
        "delay_seconds": delay,
        "category": category,
        "vehicle_id": vehicle_id,
        "predicted_time": datetime(2026, 8, 19, 21, 0, tzinfo=timezone.utc),
        "poll_timestamp": datetime(2026, 8, 19, 21, 1, tzinfo=timezone.utc),
    }


class TestSchema:
    def test_creates_table(self, tmp_path):
        db = ObservationsDB(tmp_path / "obs.db")
        try:
            row = db.conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='observations'"
            ).fetchone()
            assert row[0] == "observations"
        finally:
            db.close()

    def test_without_rowid(self, tmp_path):
        db = ObservationsDB(tmp_path / "obs.db")
        try:
            sql = db.conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='observations'"
            ).fetchone()[0]
            assert "WITHOUT ROWID" in sql
        finally:
            db.close()


class TestUpsert:
    def test_inserts_new_rows(self, tmp_path):
        db = ObservationsDB(tmp_path / "obs.db")
        try:
            db.upsert([_row()])
            db.upsert([_row(trip_id="t2")])
            assert db.count() == 2
        finally:
            db.close()

    def test_overwrite_same_pk(self, tmp_path):
        db = ObservationsDB(tmp_path / "obs.db")
        try:
            db.upsert([_row(delay=60)])
            db.upsert([_row(delay=400, category="late")])
            assert db.count() == 1
            rows = db.export_day("2026-08-19")
            assert len(rows) == 1
            assert rows[0][5] == 400  # delay_seconds
            assert rows[0][6] == "late"  # category
        finally:
            db.close()

    def test_empty_batch(self, tmp_path):
        db = ObservationsDB(tmp_path / "obs.db")
        try:
            db.upsert([])
            assert db.count() == 0
        finally:
            db.close()


class TestRollup:
    def test_route_rollup(self, tmp_path):
        db = ObservationsDB(tmp_path / "obs.db")
        try:
            db.upsert(
                [
                    _row(trip_id="t1", route_id="42", stop_id="A", delay=60),
                    _row(trip_id="t2", route_id="42", stop_id="B", delay=-120, category="early"),
                    _row(trip_id="t3", route_id="55", stop_id="C", delay=400, category="late"),
                ]
            )
            routes = db.rollup_routes("2026-08-19")
            assert routes["42"]["total_observations"] == 2
            assert routes["42"]["on_time_count"] == 1
            assert routes["42"]["early_count"] == 1
            assert routes["42"]["late_count"] == 0
            assert routes["42"]["delay_sum"] == -60
            assert routes["55"]["total_observations"] == 1
            assert routes["55"]["late_count"] == 1
        finally:
            db.close()

    def test_stop_rollup(self, tmp_path):
        db = ObservationsDB(tmp_path / "obs.db")
        try:
            db.upsert(
                [
                    _row(trip_id="t1", route_id="42", stop_id="A", delay=60),
                    _row(trip_id="t2", route_id="42", stop_id="A", delay=400, category="late"),
                ]
            )
            stops = db.rollup_stops("2026-08-19")
            assert stops["A"]["total_observations"] == 2
            assert stops["A"]["on_time_count"] == 1
            assert stops["A"]["late_count"] == 1
            assert stops["A"]["delay_sum"] == 460
        finally:
            db.close()

    def test_filters_by_service_date(self, tmp_path):
        db = ObservationsDB(tmp_path / "obs.db")
        try:
            db.upsert(
                [
                    _row(trip_id="t1", service_date=date(2026, 8, 18)),
                    _row(trip_id="t2", service_date=date(2026, 8, 19)),
                ]
            )
            routes = db.rollup_routes("2026-08-19")
            assert routes["42"]["total_observations"] == 1
        finally:
            db.close()


class TestLifecycle:
    def test_delete_service_date(self, tmp_path):
        db = ObservationsDB(tmp_path / "obs.db")
        try:
            db.upsert(
                [
                    _row(trip_id="t1", service_date=date(2026, 8, 18)),
                    _row(trip_id="t2", service_date=date(2026, 8, 19)),
                ]
            )
            db.delete_service_date("2026-08-18")
            assert db.count() == 1
            assert db.count("2026-08-19") == 1
            assert db.count("2026-08-18") == 0
        finally:
            db.close()

    def test_export_day(self, tmp_path):
        db = ObservationsDB(tmp_path / "obs.db")
        try:
            db.upsert([_row(trip_id="t1"), _row(trip_id="t2", service_date=date(2026, 8, 18))])
            rows = db.export_day("2026-08-19")
            assert len(rows) == 1
            assert rows[0][0] == "t1"  # trip_id
        finally:
            db.close()


class TestState:
    def test_defaults_when_missing(self, tmp_path):
        assert load_state(str(tmp_path / "state")) == DEFAULT_STATE

    def test_round_trip(self, tmp_path):
        d = tmp_path / "state"
        save_state(str(d), "2026-08-19", 1783374000.0)
        state = load_state(str(d))
        assert state["service_date"] == "2026-08-19"
        assert state["last_poll_ts"] == 1783374000

    def test_accepts_date_object(self, tmp_path):
        d = tmp_path / "state"
        save_state(str(d), date(2026, 8, 19), 1783374000.0)
        assert load_state(str(d))["service_date"] == "2026-08-19"