"""S3 uploader tests — boto3 is monkeypatched out; nothing hits the network.

These pin the poller↔S3 *contract* (env var names, no-default-chain creds,
cache-control semantics) rather than boto3's own behavior, which is exercised
by the live smoke cycle.
"""

import pytest

import poller.s3 as s3


class FakeClient:
    def __init__(self):
        self.calls = []
        self.existing = set()

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.calls.append((path, bucket, key, ExtraArgs))

    def head_object(self, Bucket=None, Key=None):
        self.calls.append(("head", Bucket, Key))
        if Key not in self.existing:
            raise Exception("404")
        return {}


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