"""Phase 0: storage interface must round-trip bytes exactly and never
resolve a URI outside its own base directory."""

import pytest

from backend.storage.base import StorageObjectNotFoundError
from backend.storage.local import LocalDiskStorage


def test_put_then_get_roundtrips_bytes_exactly(tmp_path):
    storage = LocalDiskStorage(tmp_path)
    data = b"not really a video, but binary-safe: \x00\x01\xff\xfe"

    uri = storage.put(data, suggested_name="clip.mp4")

    assert storage.get(uri) == data


def test_put_preserves_suggested_name_extension(tmp_path):
    storage = LocalDiskStorage(tmp_path)

    uri = storage.put(b"x", suggested_name="clip.mp4")

    assert uri.endswith(".mp4")


def test_exists_true_after_put(tmp_path):
    storage = LocalDiskStorage(tmp_path)

    uri = storage.put(b"hello")

    assert storage.exists(uri) is True


def test_exists_false_for_unknown_uri(tmp_path):
    storage = LocalDiskStorage(tmp_path)

    assert storage.exists("local://never-written") is False


def test_get_raises_for_unknown_uri(tmp_path):
    storage = LocalDiskStorage(tmp_path)

    with pytest.raises(StorageObjectNotFoundError):
        storage.get("local://never-written")


def test_get_raises_for_wrong_scheme(tmp_path):
    storage = LocalDiskStorage(tmp_path)

    with pytest.raises(StorageObjectNotFoundError):
        storage.get("s3://some-bucket/some-key")


def test_get_rejects_path_traversal(tmp_path):
    storage = LocalDiskStorage(tmp_path)
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("should never be reachable")

    with pytest.raises(StorageObjectNotFoundError):
        storage.get(f"local://../{secret.name}")


def test_base_dir_created_if_missing(tmp_path):
    nested = tmp_path / "does" / "not" / "exist" / "yet"

    storage = LocalDiskStorage(nested)

    assert nested.is_dir()
    uri = storage.put(b"data")
    assert storage.get(uri) == b"data"
