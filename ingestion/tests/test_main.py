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
    monkeypatch.setattr(main, "_upload", lambda key, path: uploaded.append(key) or True)

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
    monkeypatch.setattr(main, "_upload", lambda key, path: uploaded.append(key) or True)

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
    monkeypatch.setattr(main, "_upload", lambda key, path: False)

    main._archive_elapsed_dates(db, current_sd="2026-08-30")

    # local parquet survives a failed upload so the next cycle can retry
    assert (tmp_path / "staged.parquet").exists()
