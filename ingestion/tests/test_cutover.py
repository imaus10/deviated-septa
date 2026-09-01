"""cutover tests — S3, static, and restore are all monkeypatched out.

Verifies the orchestration contract: dry-run mutates nothing; apply drops the
partial today archive (with a --yes guard), wipes stale baseline/daily before
restoring, emits geometries, and hands restore a dry_run=False namespace.
Nothing here touches real S3, Neon, or the repo state dir.
"""

import argparse
import sys

import pytest

import poller.s3 as s3
import scripts.cutover as cutover

DATES = [
    "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-29",
    "2026-08-30", "2026-08-31", "2026-09-01",
]
TODAY = "2026-09-01"


def _fake_objects(dates):
    return {f"archive/observations/{d}.parquet": "x" for d in dates}


class FakeS3Client:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.uploads = []

    def list_objects_v2(self, Bucket=None, Prefix=None, ContinuationToken=None):
        return {
            "Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)],
            "IsTruncated": False,
        }

    def head_object(self, Bucket=None, Key=None):
        if Key not in self.objects:
            raise Exception("404")
        return {}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.objects[key] = str(path)
        self.uploads.append((key, str(path)))

    def delete_object(self, Bucket=None, Key=None):
        self.objects.pop(Key, None)


class _FakeStatic:
    def close(self):
        pass


class _NonTty:
    def isatty(self):
        return False


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "deviated-septa-prod")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "AK")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "SK")


@pytest.fixture
def patch_deps(monkeypatch, tmp_path):
    fake = FakeS3Client(_fake_objects(DATES))
    # artifacts a completed restore would have produced (restore itself is faked)
    fake.objects["public/current.json"] = "x"
    fake.objects["archive/routes.parquet"] = "x"
    fake.objects["archive/stops.parquet"] = "x"
    monkeypatch.setattr(s3, "_make_client", lambda: fake)

    monkeypatch.setattr(cutover, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cutover, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cutover, "STATIC_DB", tmp_path / "state" / "static.db")

    calls = {"check_and_update": [], "restore": []}

    def fake_check(data_dir, db_path):
        calls["check_and_update"].append((data_dir, db_path))
        return _FakeStatic(), False

    monkeypatch.setattr(cutover.gtfs_static, "check_and_update", fake_check)
    monkeypatch.setattr(
        cutover.gtfs_static,
        "load_local_metadata",
        lambda data_dir: {"routes": {}, "stops": {}, "calendar": {}},
    )
    monkeypatch.setattr(
        cutover.route_geometries,
        "build_geometries",
        lambda static, metadata: [{"route_id": "42"}],
    )
    monkeypatch.setattr(cutover.restore_state, "restore", lambda ns: calls["restore"].append(ns))
    return fake, calls


def _main_args(**kw):
    args = argparse.Namespace()
    args.apply = kw.get("apply", False)
    args.env_file = kw.get("env_file", "/dev/null")
    args.yes = kw.get("yes", False)
    args.skip_poll = kw.get("skip_poll", True)
    return args


class TestPreflight:
    def test_missing_env_raises(self, patch_deps, monkeypatch):
        monkeypatch.delenv("S3_BUCKET", raising=False)
        with pytest.raises(SystemExit, match="missing env vars"):
            cutover.preflight(_main_args())

    def test_no_archives_raises(self, patch_deps, env):
        fake, _ = patch_deps
        fake.objects = {}
        with pytest.raises(SystemExit, match="no archive"):
            cutover.preflight(_main_args())

    def test_reports_range_and_flags_today(self, patch_deps, env, capsys):
        cutover.preflight(_main_args())
        out = capsys.readouterr().out
        assert "deviated-septa-prod" in out
        assert "2026-08-26..2026-09-01" in out
        assert "partial snapshot" in out


class TestDryRun:
    def test_mutates_nothing(self, patch_deps, env):
        fake, calls = patch_deps
        cutover.main(["--skip-poll", "--env-file", "/dev/null"])

        assert calls["check_and_update"] == []           # no static build
        assert calls["restore"][-1].dry_run is True      # restore stays dry-run
        assert f"archive/observations/{TODAY}.parquet" in fake.objects  # not deleted
        assert fake.uploads == []                        # nothing uploaded


class TestApply:
    def test_drops_today_and_wipes_state_before_restore(self, patch_deps, env, tmp_path):
        fake, calls = patch_deps
        state = tmp_path / "state"
        state.mkdir(parents=True)
        (state / "all-baseline.json").write_text("{}")
        daily = state / "daily"
        daily.mkdir()
        (daily / "2026-08-30.json").write_text("{}")

        cutover.main(["--apply", "--yes", "--skip-poll", "--env-file", "/dev/null"])

        assert f"archive/observations/{TODAY}.parquet" not in fake.objects
        assert not (state / "all-baseline.json").exists()
        assert not (daily / "2026-08-30.json").exists()
        assert calls["restore"][-1].dry_run is False
        assert any(k == "public/geometries.json" for k, _ in fake.uploads)
        assert calls["check_and_update"] != []

    def test_refuses_delete_without_yes_non_interactive(self, patch_deps, env, monkeypatch):
        fake, _ = patch_deps
        monkeypatch.setattr(cutover.sys, "stdin", _NonTty())
        with pytest.raises(SystemExit, match="--yes"):
            cutover.main(["--apply", "--skip-poll", "--env-file", "/dev/null"])
        assert f"archive/observations/{TODAY}.parquet" in fake.objects  # kept

    def test_skips_delete_when_today_not_present(self, patch_deps, env):
        fake, calls = patch_deps
        fake.objects = _fake_objects(DATES[:-1])  # no 09-01
        fake.objects["public/current.json"] = "x"
        fake.objects["archive/routes.parquet"] = "x"
        fake.objects["archive/stops.parquet"] = "x"
        cutover.main(["--apply", "--yes", "--skip-poll", "--env-file", "/dev/null"])
        assert calls["restore"][-1].dry_run is False  # still restores fine