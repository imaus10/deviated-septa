from datetime import datetime, timezone

import poller.main as main
from poller.state import ObservationsDB


def _row(sd, day=1):
    return {
        "trip_id": "T1",
        "stop_sequence": 1,
        "service_date": sd,
        "route_id": "42",
        "stop_id": "S1",
        "delay_seconds": 10,
        "category": "on_time",
        "vehicle_id": None,
        "predicted_time": datetime(2026, 8, day, 8, 0, tzinfo=timezone.utc),
        "poll_timestamp": datetime(2026, 8, day, 8, 0, tzinfo=timezone.utc),
    }


def _db_with_days(tmp_path, days):
    db = ObservationsDB(tmp_path / "obs.db")
    for i, sd in enumerate(days):
        db.upsert([_row(sd, day=int(sd.split("-")[2]))])
    return db


def test_archives_elapsed_dates_and_deletes_local(monkeypatch, tmp_path):
    db = _db_with_days(tmp_path, ["2026-08-28", "2026-08-29"])
    exists = set()
    uploaded = []
    written = []

    def fake_exists(key):
        return key in exists

    def fake_write(rows, obs_dir):
        p = tmp_path / "staged.parquet"
        p.write_bytes(b"data")
        written.append((rows, obs_dir))
        return p

    monkeypatch.setattr(main.s3, "object_exists", fake_exists)
    monkeypatch.setattr(main.archives, "write_observations", fake_write)
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: uploaded.append(key) or True)

    main._archive_elapsed_dates(db, current_sd="2026-08-30")

    # both elapsed dates archived, current skipped
    assert set(uploaded) == {
        "archive/observations/2026-08-28.parquet",
        "archive/observations/2026-08-29.parquet",
    }
    # local file deleted after successful upload
    assert not (tmp_path / "staged.parquet").exists()


def test_skips_dates_already_on_s3(monkeypatch, tmp_path):
    db = _db_with_days(tmp_path, ["2026-08-28", "2026-08-29"])
    exists = {"archive/observations/2026-08-28.parquet"}
    uploaded = []

    monkeypatch.setattr(main.s3, "object_exists", lambda key: key in exists)
    monkeypatch.setattr(
        main.archives,
        "write_observations",
        lambda rows, obs_dir: (
            (tmp_path / "staged.parquet").write_bytes(b"data"),
            tmp_path / "staged.parquet",
        )[1],
    )
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: uploaded.append(key) or True)

    main._archive_elapsed_dates(db, current_sd="2026-08-30")

    # 08-28 already archived on S3 -> not re-uploaded; 08-29 still done
    assert uploaded == ["archive/observations/2026-08-29.parquet"]


def test_keeps_local_on_upload_failure(monkeypatch, tmp_path):
    db = _db_with_days(tmp_path, ["2026-08-29"])

    def fake_write(rows, obs_dir):
        p = tmp_path / "staged.parquet"
        p.write_bytes(b"data")
        return p

    monkeypatch.setattr(main.s3, "object_exists", lambda key: False)
    monkeypatch.setattr(main.archives, "write_observations", fake_write)
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: False)

    main._archive_elapsed_dates(db, current_sd="2026-08-30")

    # local parquet survives a failed upload so the next cycle can retry
    assert (tmp_path / "staged.parquet").exists()


class _FakeStatic:
    def iter_trips(self):
        return iter([("t1", "42"), ("t2", "42"), ("t3", "10")])


class _FakeDB:
    def last_service_date_for_routes(self, route_ids):
        return {"62": "2026-08-10"}


def test_refresh_static_derived_closes_dropped_route(monkeypatch, tmp_path):
    metadata = {
        "routes": {
            "42": {"route_name": "42", "route_type": 3},
            "10": {"route_name": "10", "route_type": 0},
            "62": {"route_name": "62", "route_type": 3},
        },
        "stops": {"A": {"stop_name": "A", "stop_lat": 39.95, "stop_lon": -75.16}},
        "calendar": {"wk": {"start_date": "20260823", "end_date": "20260920"}},
    }
    uploaded = []
    captured = {}

    monkeypatch.setattr(main, "STATE_DIR", tmp_path)
    monkeypatch.setattr(
        main.route_geometries,
        "build_geometries",
        lambda static, meta: [{"route_id": "42"}],
    )
    monkeypatch.setattr(main.s3, "object_exists", lambda key: False)  # no existing ledger yet
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: uploaded.append(key) or True)
    monkeypatch.setattr(
        main.archives,
        "write_routes_registry",
        lambda routes, d: captured.setdefault("routes", routes) or tmp_path / "routes.parquet",
    )
    monkeypatch.setattr(
        main.archives,
        "write_stops_registry",
        lambda stops, d: captured.setdefault("stops", stops) or tmp_path / "stops.parquet",
    )

    main._refresh_static_derived(metadata, _FakeStatic(), _FakeDB())

    assert captured["routes"]["42"]["valid_to"] is None       # active, open-ended
    assert captured["routes"]["10"]["valid_to"] is None
    assert captured["routes"]["62"]["valid_to"] == "2026-08-10"  # dropped, closed
    assert captured["routes"]["62"]["valid_from"] == "2026-08-23"  # calendar fallback
    assert "public/geometries.json" in uploaded
    assert "archive/routes.parquet" in uploaded
    assert "archive/stops.parquet" in uploaded
