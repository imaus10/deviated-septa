"""S3 uploader tests — boto3 is monkeypatched out; nothing hits the network.

These pin the poller↔S3 *contract* (env var names, no-default-chain creds,
cache-control semantics) rather than boto3's own behavior, which is exercised
by the live smoke cycle.
"""

import pathlib

import pyarrow.fs as pyarrow_fs
import pytest

import poller.s3 as s3


class FakeClient:
    def __init__(self):
        self.calls = []
        self.existing = set()
        self.objects = {}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.calls.append((path, bucket, key, ExtraArgs))
        self.objects[key] = str(path)

    def head_object(self, Bucket=None, Key=None):
        self.calls.append(("head", Bucket, Key))
        if Key not in self.existing:
            raise Exception("404")
        return {}

    def list_objects_v2(self, Bucket=None, Prefix=None, ContinuationToken=None):
        self.calls.append(("list", Bucket, Prefix, ContinuationToken))
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        if ContinuationToken == "next":
            return {
                "Contents": [{"Key": k} for k in keys[2:]],
                "IsTruncated": False,
            }
        return {
            "Contents": [{"Key": k} for k in keys[:2]],
            "IsTruncated": True,
            "NextContinuationToken": "next",
        }

    def download_file(self, bucket, key, local_path):
        self.calls.append(("download", bucket, key, local_path))
        pathlib.Path(local_path).write_bytes(b"data")

    def delete_object(self, Bucket=None, Key=None):
        self.calls.append(("delete", Bucket, Key))
        self.objects.pop(Key, None)


def _mock_client(monkeypatch, fake: FakeClient):
    monkeypatch.setattr(s3, "_make_client", lambda: fake)


def _set_env(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


class TestUploadFile:
    def test_publishes_current_json_with_cache_control(self, monkeypatch, tmp_path):
        _set_env(
            monkeypatch,
            S3_BUCKET="deviated-septa-dev",
            S3_ACCESS_KEY_ID="AK",
            S3_SECRET_ACCESS_KEY="SK",
        )
        fake = FakeClient()
        _mock_client(monkeypatch, fake)
        p = tmp_path / "current.json"
        p.write_text("{}", encoding="utf-8")

        s3.upload_file(p, "public/current.json", cache_control="max-age=55, stale-while-revalidate=5")

        assert len(fake.calls) == 1
        path, bucket, key, extra = fake.calls[0]
        assert path == str(p)
        assert bucket == "deviated-septa-dev"
        assert key == "public/current.json"
        assert extra == {
            "CacheControl": "max-age=55, stale-while-revalidate=5",
            "ContentType": "application/json",
        }


class TestObjectExists:
    def test_true_when_present(self, monkeypatch):
        _set_env(monkeypatch, S3_BUCKET="b", S3_ACCESS_KEY_ID="AK", S3_SECRET_ACCESS_KEY="SK")
        fake = FakeClient()
        fake.existing = {"archive/observations/2026-08-28.parquet"}
        _mock_client(monkeypatch, fake)

        assert s3.object_exists("archive/observations/2026-08-28.parquet") is True

    def test_false_when_absent_or_error(self, monkeypatch):
        _set_env(monkeypatch, S3_BUCKET="b", S3_ACCESS_KEY_ID="AK", S3_SECRET_ACCESS_KEY="SK")
        fake = FakeClient()
        _mock_client(monkeypatch, fake)

        assert s3.object_exists("archive/observations/2026-08-28.parquet") is False


class TestUpload:
    def test_returns_true_on_success(self, monkeypatch, tmp_path):
        _set_env(monkeypatch, S3_BUCKET="b", S3_ACCESS_KEY_ID="AK", S3_SECRET_ACCESS_KEY="SK")
        fake = FakeClient()
        _mock_client(monkeypatch, fake)
        p = tmp_path / "current.json"
        p.write_text("{}", encoding="utf-8")

        assert s3.upload("public/current.json", p) is True
        assert len(fake.calls) == 1

    def test_retries_then_returns_false(self, monkeypatch, tmp_path):
        _set_env(monkeypatch, S3_BUCKET="b", S3_ACCESS_KEY_ID="AK", S3_SECRET_ACCESS_KEY="SK")
        fake = FakeClient()
        attempts = []

        def boom(path, bucket, key, ExtraArgs=None):
            attempts.append(key)
            raise RuntimeError("boom")

        fake.upload_file = boom
        _mock_client(monkeypatch, fake)
        p = tmp_path / "current.json"
        p.write_text("{}", encoding="utf-8")

        assert s3.upload("public/current.json", p) is False
        assert attempts == ["public/current.json", "public/current.json"]


class TestListObjects:
    def test_paginates(self, monkeypatch):
        _set_env(monkeypatch, S3_BUCKET="b", S3_ACCESS_KEY_ID="AK", S3_SECRET_ACCESS_KEY="SK")
        fake = FakeClient()
        for i in range(5):
            fake.objects[f"archive/observations/2026-08-{i+1:02d}.parquet"] = "x"
        _mock_client(monkeypatch, fake)

        keys = s3.list_objects("archive/observations/")

        assert len(keys) == 5
        assert all(k.startswith("archive/observations/") for k in keys)
        assert ("list", "b", "archive/observations/", None) in fake.calls
        assert ("list", "b", "archive/observations/", "next") in fake.calls


class TestDownloadFile:
    def test_writes_local(self, monkeypatch, tmp_path):
        _set_env(monkeypatch, S3_BUCKET="b", S3_ACCESS_KEY_ID="AK", S3_SECRET_ACCESS_KEY="SK")
        fake = FakeClient()
        _mock_client(monkeypatch, fake)
        dest = tmp_path / "out.parquet"

        s3.download_file(dest, "archive/observations/2026-08-28.parquet")

        assert dest.read_bytes() == b"data"
        assert fake.calls[0][:3] == ("download", "b", "archive/observations/2026-08-28.parquet")


class TestDeleteObject:
    def test_deletes_key(self, monkeypatch):
        _set_env(monkeypatch, S3_BUCKET="b", S3_ACCESS_KEY_ID="AK", S3_SECRET_ACCESS_KEY="SK")
        fake = FakeClient()
        fake.objects["archive/observations/2026-09-01.parquet"] = "x"
        _mock_client(monkeypatch, fake)

        s3.delete_object("archive/observations/2026-09-01.parquet")

        assert fake.calls[0][:3] == ("delete", "b", "archive/observations/2026-09-01.parquet")
        assert "archive/observations/2026-09-01.parquet" not in fake.objects


class TestMakeClient:
    def test_requires_credentials(self, monkeypatch):
        monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)
        with pytest.raises(KeyError):
            s3._make_client()

    def test_explicit_creds_not_chain(self, monkeypatch):
        _set_env(monkeypatch, S3_ACCESS_KEY_ID="AK", S3_SECRET_ACCESS_KEY="SK")
        calls = []

        def fake_client(*args, **kwargs):
            calls.append((args, kwargs))
            return object()

        monkeypatch.setattr(s3.boto3, "client", fake_client)

        s3._make_client()

        args, kwargs = calls[0]
        assert args == ("s3",)
        assert kwargs["aws_access_key_id"] == "AK"
        assert kwargs["aws_secret_access_key"] == "SK"
        assert kwargs["region_name"] == "us-east-1"
        assert "profile_name" not in kwargs and "ChainProvider" not in str(kwargs)


class TestFilesystem:
    def test_builds_s3_filesystem_with_explicit_creds(self, monkeypatch):
        _set_env(monkeypatch, S3_ACCESS_KEY_ID="AK", S3_SECRET_ACCESS_KEY="SK")
        fs = s3.filesystem()
        assert isinstance(fs, pyarrow_fs.S3FileSystem)
        assert fs.region == "us-east-1"

    def test_requires_credentials(self, monkeypatch):
        monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)
        with pytest.raises(KeyError):
            s3.filesystem()

    def test_full_path_prepends_bucket(self, monkeypatch):
        _set_env(monkeypatch, S3_BUCKET="deviated-septa-prod")
        assert s3.full_path("archive/observations/2026-08-26.parquet") == (
            "deviated-septa-prod/archive/observations/2026-08-26.parquet"
        )