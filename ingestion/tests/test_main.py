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


def test_delete_service_date_batches(tmp_path):
    db = ObservationsDB(tmp_path / "obs.db")
    rows = [{**_row("2026-08-28"), "trip_id": f"T{i}"} for i in range(25)]
    rows.append(_row("2026-08-29"))
    db.upsert(rows)

    n = db.delete_service_date("2026-08-28", batch=10)  # 3 batches

    assert n == 25
    assert db.count("2026-08-28") == 0
    assert db.count("2026-08-29") == 1  # other dates untouched


def test_archives_elapsed_dates_and_deletes_local(monkeypatch, tmp_path):
    db = _db_with_days(tmp_path, ["2026-08-28", "2026-08-29"])
    uploaded = []
    written = []

    def fake_write(rows, obs_dir, **kw):
        p = tmp_path / "staged.parquet"
        p.write_bytes(b"data")
        written.append((rows, obs_dir, kw.get("as_of_poll")))
        return p

    monkeypatch.setattr(main.archives, "read_archive_meta", lambda *a, **k: (None, None))
    monkeypatch.setattr(main.archives, "write_observations", fake_write)
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: uploaded.append(key) or True)

    main._archive_elapsed_dates(db, present={"2026-08-30"})

    # both elapsed dates archived (feed is on 08-30, both are quiet), local deleted
    assert set(uploaded) == {
        "archive/observations/2026-08-28.parquet",
        "archive/observations/2026-08-29.parquet",
    }
    assert all(ap is not None for _, _, ap in written)  # as_of_poll baked into metadata
    assert not (tmp_path / "staged.parquet").exists()


def test_skips_dates_already_archived_through(monkeypatch, tmp_path):
    db = _db_with_days(tmp_path, ["2026-08-28", "2026-08-29"])
    uploaded = []

    monkeypatch.setattr(
        main.archives,
        "read_archive_meta",
        lambda path, filesystem=None: (10 ** 12, 1) if "2026-08-28.parquet" in path else (None, None),
    )
    monkeypatch.setattr(
        main.archives,
        "write_observations",
        lambda rows, obs_dir, **kw: (
            (tmp_path / "staged.parquet").write_bytes(b"data"),
            tmp_path / "staged.parquet",
        )[1],
    )
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: uploaded.append(key) or True)

    main._archive_elapsed_dates(db, present={"2026-08-30"})

    # 08-28 already archived through its max poll -> not re-uploaded; 08-29 done
    assert uploaded == ["archive/observations/2026-08-29.parquet"]


def test_rearchives_when_store_advanced_past_archive(monkeypatch, tmp_path):
    db = _db_with_days(tmp_path, ["2026-08-29"])
    uploaded = []

    monkeypatch.setattr(main.archives, "read_archive_meta", lambda *a, **k: (0, 1))  # stale, store holds 1
    monkeypatch.setattr(
        main.archives,
        "write_observations",
        lambda rows, obs_dir, **kw: (
            (tmp_path / "staged.parquet").write_bytes(b"data"),
            tmp_path / "staged.parquet",
        )[1],
    )
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: uploaded.append(key) or True)

    main._archive_elapsed_dates(db, present={"2026-08-30"})

    assert uploaded == ["archive/observations/2026-08-29.parquet"]


def test_skips_rearchive_when_store_is_partial(monkeypatch, tmp_path):
    db = _db_with_days(tmp_path, ["2026-08-29"])
    uploaded = []

    monkeypatch.setattr(
        main.archives,
        "read_archive_meta",
        lambda *a, **k: (0, 1000),  # archive has 1000 rows; store has 1 (mid-drain)
    )
    monkeypatch.setattr(
        main.archives,
        "write_observations",
        lambda rows, obs_dir, **kw: (
            (tmp_path / "staged.parquet").write_bytes(b"data"),
            tmp_path / "staged.parquet",
        )[1],
    )
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: uploaded.append(key) or True)

    main._archive_elapsed_dates(db, present={"2026-08-30"})

    assert uploaded == []  # refused to overwrite a complete archive from a partial store


def test_skips_rearchive_when_archive_footer_unreadable(monkeypatch, tmp_path, capsys):
    """Regression: a transient footer read must never be read as 'not archived'.

    On 2026-09-08 a read error returned (None, None), which silently disabled
    both the as_of_poll check and the mid-drain row-count guard, so a partial
    store overwrote a complete archive (770,789 -> 36,449 rows).
    """
    db = _db_with_days(tmp_path, ["2026-08-29"])
    uploaded = []
    written = []

    def boom(*a, **k):
        raise TimeoutError("S3 timed out")

    monkeypatch.setattr(main.archives, "read_archive_meta", boom)
    monkeypatch.setattr(
        main.archives,
        "write_observations",
        lambda rows, obs_dir, **kw: written.append(rows) or (tmp_path / "staged.parquet"),
    )
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: uploaded.append(key) or True)

    main._archive_elapsed_dates(db, present={"2026-08-30"})

    assert uploaded == []   # archive left untouched
    assert written == []   # and nothing even staged
    assert "refusing to overwrite" in capsys.readouterr().out


def test_archives_when_archive_absent_not_found(monkeypatch, tmp_path):
    """FileNotFoundError from the footer read means 'nothing archived yet'."""
    db = _db_with_days(tmp_path, ["2026-08-29"])
    uploaded = []

    def missing(*a, **k):
        raise FileNotFoundError("no such key")

    monkeypatch.setattr(main.archives, "read_archive_meta", missing)
    monkeypatch.setattr(
        main.archives,
        "write_observations",
        lambda rows, obs_dir, **kw: (
            (tmp_path / "staged.parquet").write_bytes(b"data"),
            tmp_path / "staged.parquet",
        )[1],
    )
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: uploaded.append(key) or True)

    main._archive_elapsed_dates(db, present={"2026-08-30"})

    assert uploaded == ["archive/observations/2026-08-29.parquet"]


def test_no_archive_while_date_in_feed(monkeypatch, tmp_path):
    db = _db_with_days(tmp_path, ["2026-08-29"])
    uploaded = []
    monkeypatch.setattr(main.archives, "read_archive_meta", lambda *a, **k: (None, None))
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: uploaded.append(key) or True)

    # 08-29 is still in the feed -> min(present)=08-29, so 08-29 is not < min
    main._archive_elapsed_dates(db, present={"2026-08-29", "2026-08-30"})

    assert uploaded == []


def test_no_archive_while_not_quiet(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    db = ObservationsDB(tmp_path / "obs.db")
    row = _row("2026-08-29")
    row["poll_timestamp"] = datetime.now(timezone.utc)  # updated just now -> not quiet
    db.upsert([row])
    uploaded = []

    monkeypatch.setattr(main.archives, "read_archive_meta", lambda *a, **k: (None, None))
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: uploaded.append(key) or True)

    main._archive_elapsed_dates(db, present={"2026-08-30"})

    assert uploaded == []  # feed moved past 08-29 but it's still being updated


def test_keeps_local_on_upload_failure(monkeypatch, tmp_path):
    db = _db_with_days(tmp_path, ["2026-08-29"])

    def fake_write(rows, obs_dir, **kw):
        p = tmp_path / "staged.parquet"
        p.write_bytes(b"data")
        return p

    monkeypatch.setattr(main.archives, "read_archive_meta", lambda *a, **k: (None, None))
    monkeypatch.setattr(main.archives, "write_observations", fake_write)
    monkeypatch.setattr(main.s3, "upload", lambda key, path, **meta: False)

    main._archive_elapsed_dates(db, present={"2026-08-30"})

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
