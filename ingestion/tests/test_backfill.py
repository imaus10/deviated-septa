"""backfill_daily tests — seed raw archives, rebuild the missing daily chronicle
entry, and assert the guards refuse the cases they must never publish.
"""

import argparse
import json
from datetime import date

import pytest

import poller.archives as archives
import poller.s3 as s3
import scripts.backfill_daily as backfill_daily

SD = "2026-09-09"
PREV = "2026-09-08"
NEXT = "2026-09-10"


def _row(trip_id, delay, category, poll_ts, service_date):
    return (
        trip_id, 1, service_date, "bus42", "S1", delay, category, None, poll_ts, poll_ts,
    )


def _rows(service_date, count=3):
    base = date.fromisoformat(service_date).toordinal()
    rows = [
        _row(f"t{service_date}a", 60, "on_time", base, service_date),
        _row(f"t{service_date}b", 400, "late", base + 1, service_date),
        _row(f"t{service_date}c", -120, "early", base + 2, service_date),
    ]
    return rows[:count]


class _NoSuchKey(Exception):
    pass


class _Exceptions:
    NoSuchKey = _NoSuchKey


class _Body:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload


class FakeS3Client:
    """Minimal S3 double: get/upload/list over an in-memory object map."""

    exceptions = _Exceptions()

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []

    def get_object(self, Bucket=None, Key=None):
        if Key not in self.objects:
            raise _NoSuchKey(f"no such key: {Key}")
        return {"Body": _Body(self.objects[Key])}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        with open(path, "rb") as fh:
            self.objects[key] = fh.read()
        self.uploads.append(key)

    def list_objects_v2(self, Bucket=None, Prefix=None, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Seed 3-row archives for 09-08/09-09/09-10 and point the script at them.

    The archive parquet files are real (written by archives.write_observations
    and read back through the real streaming footer/iter_batches code paths);
    only the S3 key names and the filesystem handle are faked.
    """
    archive_dir = tmp_path / "seed_archives"
    for sd in (PREV, SD, NEXT):
        archives.write_observations(_rows(sd), archive_dir, as_of_poll=1)

    def stream_from_seed(key, filesystem=None):
        yield from archives.stream_observation(archive_dir / key.rsplit("/", 1)[-1])

    def meta_from_seed(key, filesystem=None):
        return archives.read_archive_meta(archive_dir / key.rsplit("/", 1)[-1])

    fake = FakeS3Client()
    for sd in (PREV, SD, NEXT):
        fake.objects[f"archive/observations/{sd}.parquet"] = (
            archive_dir / f"{sd}.parquet"
        ).read_bytes()

    monkeypatch.setattr(s3, "_make_client", lambda: fake)
    monkeypatch.setattr(s3, "filesystem", lambda: object())
    monkeypatch.setattr(backfill_daily, "stream_observation", stream_from_seed)
    monkeypatch.setattr(backfill_daily, "read_archive_meta", meta_from_seed)
    monkeypatch.setattr(backfill_daily, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("S3_BUCKET", "deviated-septa-dev")
    return fake


def _args(**kw):
    base = {"date": SD, "dry_run": False, "min_rows": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _short_target(monkeypatch, tmp_path, name):
    """Swap the target date's archive for a 1-row one (both stream and footer).

    The archive stays internally consistent, so this is exactly the shape of a
    real short raw ledger (2026-09-08): the neighbour check is the only thing
    that can catch it.
    """
    scratch = tmp_path / name
    archives.write_observations(_rows(SD, count=1), scratch, as_of_poll=1)
    seed_dir = tmp_path / "seed_archives"

    def _target(key):
        return key.rsplit("/", 1)[-1] == f"{SD}.parquet"

    def one_row_stream(key, filesystem=None):
        src = scratch if _target(key) else seed_dir
        yield from archives.stream_observation(src / key.rsplit("/", 1)[-1])

    def one_row_meta(key, filesystem=None):
        src = scratch if _target(key) else seed_dir
        return archives.read_archive_meta(src / key.rsplit("/", 1)[-1])

    monkeypatch.setattr(backfill_daily, "stream_observation", one_row_stream)
    monkeypatch.setattr(backfill_daily, "read_archive_meta", one_row_meta)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_backfills_missing_daily_from_archive(env):
    assert backfill_daily.backfill(_args()) == 0

    key = f"state/daily/{SD}.json"
    assert key in env.uploads
    daily = json.loads(env.objects[key])
    assert daily["service_date"] == SD
    assert daily["as_of_poll"] == 1
    assert daily["routes"]["bus42"] == {
        "total_observations": 3,
        "on_time_count": 1,
        "early_count": 1,
        "late_count": 1,
        "delay_sum": 60 + 400 - 120,
    }
    assert daily["stops"]["S1"]["total_observations"] == 3


def test_writes_local_copy(env, tmp_path):
    backfill_daily.backfill(_args())
    local = tmp_path / "state" / "daily" / f"{SD}.json"
    assert local.exists()
    assert json.loads(local.read_text())["service_date"] == SD


def test_dry_run_writes_nothing(env, tmp_path):
    assert backfill_daily.backfill(_args(dry_run=True)) == 0
    assert env.uploads == []
    assert not (tmp_path / "state" / "daily" / f"{SD}.json").exists()


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_refuses_to_overwrite_existing_daily(env):
    env.objects[f"state/daily/{SD}.json"] = json.dumps(
        {"service_date": SD, "routes": {"bus42": {"total_observations": 99}}, "stops": {}}
    ).encode()
    with pytest.raises(SystemExit) as exc:
        backfill_daily.backfill(_args())
    assert "already exists" in str(exc.value)
    assert env.uploads == []


def test_refuses_when_archive_is_missing(env, monkeypatch):
    def missing(key, filesystem=None):
        raise FileNotFoundError(key)

    monkeypatch.setattr(backfill_daily, "read_archive_meta", missing)
    with pytest.raises(SystemExit) as exc:
        backfill_daily.backfill(_args())
    assert "No raw archive" in str(exc.value)


def test_refuses_when_footer_is_unreadable(env, monkeypatch):
    def boom(key, filesystem=None):
        raise RuntimeError("S3 timeout")

    monkeypatch.setattr(backfill_daily, "read_archive_meta", boom)
    with pytest.raises(SystemExit) as exc:
        backfill_daily.backfill(_args())
    assert "unknown completeness" in str(exc.value)


def test_refuses_on_row_count_mismatch(env, monkeypatch):
    real = backfill_daily.read_archive_meta

    def lying(key, filesystem=None):
        as_of, rows = real(key, filesystem=filesystem)
        return as_of, rows + 10

    monkeypatch.setattr(backfill_daily, "read_archive_meta", lying)
    with pytest.raises(SystemExit) as exc:
        backfill_daily.backfill(_args())
    assert "footer claims" in str(exc.value)
    assert env.uploads == []


def test_refuses_short_archive_next_to_healthy_neighbours(env, monkeypatch, tmp_path):
    """A 1-row archive beside 3-row neighbours reads as a short raw ledger."""
    _short_target(monkeypatch, tmp_path, "short")
    with pytest.raises(SystemExit) as exc:
        backfill_daily.backfill(_args())
    assert "known-short raw ledger" in str(exc.value)
    assert env.uploads == []


def test_min_rows_override_still_enforces_floor(env, monkeypatch, tmp_path):
    _short_target(monkeypatch, tmp_path, "short2")
    with pytest.raises(SystemExit) as exc:
        backfill_daily.backfill(_args(min_rows=5))
    assert "below --min-rows" in str(exc.value)


def test_min_rows_override_accepts_expected_short_archive(env, monkeypatch, tmp_path):
    _short_target(monkeypatch, tmp_path, "short3")
    assert backfill_daily.backfill(_args(min_rows=1)) == 0
    assert f"state/daily/{SD}.json" in env.uploads


def test_rejects_invalid_date(env):
    with pytest.raises(SystemExit) as exc:
        backfill_daily.backfill(_args(date="09/09/2026"))
    assert "not a valid YYYY-MM-DD" in str(exc.value)