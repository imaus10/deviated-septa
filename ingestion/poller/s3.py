"""S3 upload client for the poller.

Credentials come from explicit env vars (S3_ACCESS_KEY_ID /
S3_SECRET_ACCESS_KEY) so boto3 never falls back to the default credential
chain and accidentally uploads with the admin key. S3 is best-effort: the
local state/ files are always the source of truth, so upload failures warn
but never crash the poll cycle.
"""

import os

import boto3
import pyarrow.fs as pa_fs

DEFAULT_REGION = "us-east-1"
# Safe-ish timeouts for ranged parquet reads over the Pi's flaky WiFi: a stuck
# request fails fast instead of hanging a cron/restore path indefinitely.
FS_CONNECT_TIMEOUT = 10.0
FS_REQUEST_TIMEOUT = 120.0


def full_path(key: str) -> str:
    """Absolute S3 path (`bucket/key`) for pyarrow filesystem reads.

    pyarrow's S3FileSystem treats the first path component as the bucket, so
    archive keys need the bucket name prepended.
    """
    return f"{os.environ['S3_BUCKET']}/{key}"


def filesystem() -> pa_fs.S3FileSystem:
    """pyarrow filesystem for reading parquet directly from S3.

    Explicit creds from the S3_* env vars (never the default credential chain),
    matching _make_client. Used by restore to stream archives without staging
    them on disk.
    """
    return pa_fs.S3FileSystem(
        access_key=os.environ["S3_ACCESS_KEY_ID"],
        secret_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region=DEFAULT_REGION,
        connect_timeout=FS_CONNECT_TIMEOUT,
        request_timeout=FS_REQUEST_TIMEOUT,
    )


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


def upload(key: str, path, **meta) -> bool:
    """Best-effort upload with a single retry; local file always stays truth.

    S3 is non-authoritative, so a failed upload only warns rather than failing
    the poll cycle. Returns True on success, False if the upload failed (after
    one retry).
    """
    for attempt in range(2):
        try:
            upload_file(path, key, **meta)
            return True
        except Exception as e:
            last_err = e
    print(f"  [s3] upload failed for {key}: {last_err}", flush=True)
    return False


def list_objects(prefix: str, *, bucket: str | None = None, client=None) -> list[str]:
    """List object keys under a prefix, handling pagination."""
    bucket = bucket or os.environ["S3_BUCKET"]
    client = client or _make_client()
    keys = []
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    while True:
        resp = client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            keys.append(obj["Key"])
        if not resp.get("IsTruncated"):
            return keys
        kwargs["ContinuationToken"] = resp.get("NextContinuationToken")


def download_file(
    local_path,
    key: str,
    *,
    bucket: str | None = None,
    client=None,
) -> None:
    """Download s3://bucket/key to a local file."""
    bucket = bucket or os.environ["S3_BUCKET"]
    client = client or _make_client()
    client.download_file(bucket, key, str(local_path))


def delete_object(
    key: str,
    *,
    bucket: str | None = None,
    client=None,
) -> None:
    """Delete s3://bucket/key. Used by cutover to drop a partial today archive."""
    bucket = bucket or os.environ["S3_BUCKET"]
    client = client or _make_client()
    client.delete_object(Bucket=bucket, Key=key)