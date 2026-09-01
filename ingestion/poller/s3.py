"""S3 upload client for the poller.

Credentials come from explicit env vars (S3_ACCESS_KEY_ID /
S3_SECRET_ACCESS_KEY) so boto3 never falls back to the default credential
chain and accidentally uploads with the admin key. S3 is best-effort: the
local state/ files are always the source of truth, so upload failures warn
but never crash the poll cycle.
"""

import os

import boto3

DEFAULT_REGION = "us-east-1"


def _make_client() -> boto3.client:
    return boto3.client(
        "s3",
        region_name=DEFAULT_REGION,
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
    )


def upload_file(
    local_path,
    key: str,
    *,
    bucket: str | None = None,
    client=None,
    cache_control: str | None = None,
    content_type: str = "application/json",
) -> None:
    """Upload a local file to s3://bucket/key."""
    bucket = bucket or os.environ["S3_BUCKET"]
    client = client or _make_client()
    extra = {}
    if cache_control:
        extra["CacheControl"] = cache_control
    if content_type:
        extra["ContentType"] = content_type
    client.upload_file(str(local_path), bucket, key, ExtraArgs=extra or None)


def object_exists(key: str, *, bucket: str | None = None, client=None) -> bool:
    """True if s3://bucket/key exists. False on any error (incl. 404).

    Errors are treated as "does not exist" so callers skip/re-upload without
    blocking on a transient S3 hiccup.
    """
    bucket = bucket or os.environ["S3_BUCKET"]
    client = client or _make_client()
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False