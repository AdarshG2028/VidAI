"""S3Storage (backend/storage/s3.py), mocked via moto so these run with no
real AWS account or network access -- the same guarantee LocalDiskStorage's
tests get for free from tmp_path.

Mirrors test coverage that would exist for LocalDiskStorage: put/get round
trip, not-found behaviour, delete idempotency, size for Range support, and
the same hostile-URI list test_artifacts_api.py uses for the local
backend, proving both backends reject a garbled/malicious key the same way.
"""

import boto3
import pytest
from moto import mock_aws

from backend.storage.base import StorageObjectNotFoundError
from backend.storage.s3 import S3Storage

_BUCKET = "setu-test-bucket"
_REGION = "us-east-1"


@pytest.fixture
def s3(tmp_path):
    with mock_aws():
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
        yield S3Storage(
            bucket=_BUCKET,
            region_name=_REGION,
            access_key_id="testing",
            secret_access_key="testing",
        )


def test_put_then_get_round_trips_the_bytes(s3: S3Storage) -> None:
    uri = s3.put(b"hello world", suggested_name="clip.mp4")

    assert uri.startswith("s3://")
    assert uri.endswith(".mp4")
    assert s3.get(uri) == b"hello world"


def test_put_file_round_trips_a_file_on_disk(s3: S3Storage, tmp_path) -> None:
    source = tmp_path / "source.srt"
    source.write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nhi\n")

    uri = s3.put_file(source, suggested_name="captions.srt")

    assert uri.endswith(".srt")
    assert s3.get(uri) == b"1\n00:00:00,000 --> 00:00:01,000\nhi\n"


def test_get_unknown_uri_raises_not_found(s3: S3Storage) -> None:
    with pytest.raises(StorageObjectNotFoundError):
        s3.get("s3://deadbeefdeadbeefdeadbeefdeadbeef.mp4")


def test_size_unknown_uri_raises_not_found(s3: S3Storage) -> None:
    with pytest.raises(StorageObjectNotFoundError):
        s3.size("s3://deadbeefdeadbeefdeadbeefdeadbeef.mp4")


def test_open_stream_unknown_uri_raises_not_found(s3: S3Storage) -> None:
    with pytest.raises(StorageObjectNotFoundError):
        s3.open_stream("s3://deadbeefdeadbeefdeadbeefdeadbeef.mp4")


def test_size_matches_the_stored_length(s3: S3Storage) -> None:
    uri = s3.put(b"x" * 12345)
    assert s3.size(uri) == 12345


def test_open_stream_reads_the_same_bytes_as_get(s3: S3Storage) -> None:
    uri = s3.put(b"streamed bytes")
    handle = s3.open_stream(uri)
    try:
        assert handle.read() == b"streamed bytes"
    finally:
        handle.close()


def test_exists_is_true_for_a_stored_object_false_otherwise(s3: S3Storage) -> None:
    uri = s3.put(b"data")
    assert s3.exists(uri) is True
    assert s3.exists("s3://deadbeefdeadbeefdeadbeefdeadbeef.mp4") is False


def test_delete_removes_the_object_and_is_idempotent(s3: S3Storage) -> None:
    uri = s3.put(b"data")

    assert s3.delete(uri) is True
    assert s3.exists(uri) is False
    # Already gone -- the second call reports that rather than erroring,
    # matching LocalDiskStorage's documented idempotent-delete contract.
    assert s3.delete(uri) is False


def test_delete_unknown_uri_returns_false(s3: S3Storage) -> None:
    assert s3.delete("s3://deadbeefdeadbeefdeadbeefdeadbeef.mp4") is False


def test_presigned_url_is_returned_for_a_stored_object(s3: S3Storage) -> None:
    uri = s3.put(b"data")
    url = s3.presigned_url(uri)
    assert url is not None
    assert url.startswith("http")
    assert _BUCKET in url


def test_presigned_url_is_none_for_a_hostile_or_foreign_uri(s3: S3Storage) -> None:
    assert s3.presigned_url("local://a.mp4") is None
    assert s3.presigned_url("s3://../../../etc/passwd") is None


@pytest.mark.parametrize(
    "hostile",
    [
        "s3://../../../etc/passwd",
        "s3://..\\..\\windows\\system32\\config\\sam",
        "s3://subdir/escape.mp4",
        "local:///etc/passwd",
        "s3://..",
        "s3://",
    ],
)
def test_traversal_and_foreign_uris_are_rejected(s3: S3Storage, hostile: str) -> None:
    """Same hostile-URI list as test_artifacts_api.py's
    test_traversal_and_foreign_schemes_are_rejected -- both backends must
    refuse a garbled/malicious key identically."""
    with pytest.raises(StorageObjectNotFoundError):
        s3.get(hostile)
    assert s3.exists(hostile) is False
    assert s3.presigned_url(hostile) is None
