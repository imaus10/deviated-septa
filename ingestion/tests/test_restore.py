"""restore_state tests — seed a parquet archive (int-shaped rows), run restore
against a temp state dir, and assert the rebuilt store + baseline + current.json
match the live poller's local state.
"""

import json
from datetime import date

import pytest

import scripts.restore_state as restore_state
import poller.archives as archives
import poller.s3 as s3
from poller.state import ObservationsDB

STATIC = {
    "routes": {
        "bus42": {"route_name": "42", "route_type": 3},
    },
    "stops": {
        "S1": {"stop_name": "Front & Chestnut", "stop_lat": 39.952, "stop_lon": -75.165},
    },
}

DATES = [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27),
         date(2026, 8, 28), date(2026, 8, 29), date(2026, 8, 30), date(2026, 8, 31),
         date(2026, 9, 1)]  # 08-24 .. 09-01 (9 dates)
CURRENT_SD = "2026-09-01"
WINDOW_DAYS = DATES[-7:]  # 08-26 .. 09-01
FOLDED = DATES[:-7]  # 08-24, 08-25


def _row(trip_id, delay, category, poll_ts, service_date):
    return (
        trip_id, 1, service_date, "bus42", "S1", delay, category, None, poll_ts, poll_ts,
    )


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Seed 9 archive parquet dates + monkeypatch restore deps.

    Restore streams via `stream_observation`, which restore_state imports at
    module scope; we redirect that to the local seed files (real pyarrow
    streaming, no S3). `s3.filesystem()` itself is still exercised — it is
    constructed (with fake creds) under the covers of every apply run.
    """
    archive_dir = tmp_path / "seed_archives"
    rows_by_date = {}
    for d in DATES:
        base = d.toordinal()
        rows = [
            _row(f"t{d.isoformat()}a", 60, "on_time", base, d.isoformat()),
            _row(f"t{d.isoformat()}b", 400, "late", base, d.isoformat()),
            _row(f"t{d.isoformat()}c", -120, "early", base, d.isoformat()),
        ]
        archives.write_observations(rows, archive_dir)
        rows_by_date[d] = rows

    def stream_from_seed(key, filesystem=None):
        path = archive_dir / key.rsplit("/", 1)[-1]
        yield from archives.stream_observation(path)

    fake = FakeS3Client()
    for d in DATES:
        p = archive_dir / f"{d.isoformat()}.parquet"
        fake.objects[f"archive/observations/{d.isoformat()}.parquet"] = str(p)

    monkeypatch.setattr(s3, "_make_client", lambda: fake)
    monkeypatch.setattr(restore_state, "stream_observation", stream_from_seed)
    monkeypatch.setenv("S3_BUCKET", "deviated-septa-dev")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "AK")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "SK")

    monkeypatch.setattr(restore_state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(restore_state, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(restore_state, "load_local_metadata", lambda _dir: STATIC)
    return fake


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.uploads = []

    def list_objects_v2(self, Bucket=None, Prefix=None, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.uploads.append((key, str(path)))


def test_restore_builds_7_window_store_and_folds_old(fake, tmp_path):
    restore_state.restore(argparse_namespace())

    db = ObservationsDB(tmp_path / "state" / "observations.db")
    stats = db.service_date_stats()
    store_dates = [sd for sd, _ in stats]
    assert store_dates == [d.isoformat() for d in WINDOW_DAYS]

    baseline = restore_state.load_baseline(tmp_path / "state")
    assert baseline["routes"]["bus42"]["total_observations"] == 2 * 3
    assert baseline["min_service_date"] == FOLDED[0].isoformat()
    assert baseline["max_service_date"] == FOLDED[-1].isoformat()
    db.close()


def test_restore_current_json_periods(fake, tmp_path):
    restore_state.restore(argparse_namespace())

    current = json.loads((tmp_path / "state" / "current.json").read_text())
    assert current["current_service_date"] == CURRENT_SD

    day = current["periods"]["day"]["routes"]["bus42"]["total_observations"]
    assert day == 3

    assert current["periods"]["week"]["routes"]["bus42"]["total_observations"] == 7 * 3

    all_routes = current["periods"]["all"]["routes"]["bus42"]
    assert all_routes["total_observations"] == 9 * 3


def test_restore_dry_run_writes_nothing(tmp_path, fake):
    restore_state.restore(argparse_namespace(dry_run=True))

    assert not (tmp_path / "state" / "observations.db").exists()
    assert not (tmp_path / "state" / "current.json").exists()


def test_restore_uploads_artifacts(fake, tmp_path):
    restore_state.restore(argparse_namespace())

    keys = {k for k, _ in fake.uploads}
    assert "public/current.json" in keys
    assert "state/all-baseline.json" in keys
    for d in WINDOW_DAYS[:-1]:
        assert f"state/daily/{d.isoformat()}.json" in keys


def test_restore_single_date(fake, tmp_path):
    restore_state.restore(argparse_namespace(date=FOLDED[0]))

    db = ObservationsDB(tmp_path / "state" / "observations.db")
    stats = db.service_date_stats()
    assert [sd for sd, _ in stats] == [FOLDED[0].isoformat()]
    db.close()


class _NS:
    pass


def argparse_namespace(**kw):
    ns = _NS()
    ns.dry_run = kw.get("dry_run", False)
    ns.date = kw.get("date", None)
    return ns
