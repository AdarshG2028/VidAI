"""Storage interface for uploaded videos and per-stage artifacts.

The URI a backend returns from put() is opaque by design: callers store it
and pass it back to get()/exists(), but never parse or construct one
themselves. That's what lets a later S3-backed implementation swap in
behind this same interface without touching anything that calls it.
"""

import uuid
from abc import ABC, abstractmethod
from os import PathLike
from pathlib import Path
from typing import BinaryIO


class StorageObjectNotFoundError(Exception):
    """Raised by get()/exists()/open_stream() when a URI isn't one this
    backend wrote."""


def generate_key(suggested_name: str | None) -> str:
    """The one place a storage key is minted, so every backend produces
    keys of an identical shape: a uuid4 hex, optionally followed by
    suggested_name's extension. Shared so both LocalDiskStorage and
    S3Storage can validate an inbound key against the same "could this
    possibly be one we generated" check before touching the filesystem or
    issuing an S3 call.
    """
    suffix = Path(suggested_name).suffix if suggested_name else ""
    return f"{uuid.uuid4().hex}{suffix}"


def is_safe_key(key: str) -> bool:
    """Whether a key could plausibly be one generate_key() produced --
    rejects path separators and relative segments outright, which is what
    stands between a hostile or garbled URI (path traversal, a foreign
    scheme's leftovers) and a filesystem/S3 lookup. Not a full
    reconstruction of the uuid4-hex shape: an unknown-but-safe-looking key
    still reaches the backend, which then reports it as not found rather
    than invalid -- the same externally-visible 404 either way.
    """
    return "/" not in key and "\\" not in key and key not in ("", ".", "..")


class Storage(ABC):
    @abstractmethod
    def put(self, data: bytes, *, suggested_name: str | None = None) -> str:
        """Persist data and return an opaque URI identifying it.

        suggested_name is a hint (e.g. for preserving a file extension) —
        the backend decides the actual key, so callers never assume the
        returned URI resembles the suggested name.
        """

    @abstractmethod
    def put_file(self, path: "PathLike[str] | str", *, suggested_name: str | None = None) -> str:
        """Persist a file already on disk, without reading it into memory.

        Every capability writes its output to a temp file and then stores
        it, so put(path.read_bytes()) meant holding the whole render in
        RAM purely to hand it over. An S3 backend maps this onto
        upload_file, which also gets multipart uploads for free.
        """

    @abstractmethod
    def get(self, uri: str) -> bytes:
        """Return exactly what was put() under this URI.

        Raises StorageObjectNotFoundError if the URI is unknown.
        """

    @abstractmethod
    def delete(self, uri: str) -> bool:
        """Remove a stored object. Returns whether it was there to remove.

        Idempotent by design: cleanup re-runs, and an object already gone
        is the desired end state, not an error.
        """

    @abstractmethod
    def exists(self, uri: str) -> bool:
        """Whether this URI refers to something this backend has stored."""

    @abstractmethod
    def size(self, uri: str) -> int:
        """Size in bytes.

        Needed to serve HTTP range requests: both Content-Length and the
        total in Content-Range require knowing it up front, and a video
        element cannot seek without them. An S3-backed implementation gets
        this from head_object rather than by reading the object.

        Raises StorageObjectNotFoundError if the URI is unknown.
        """

    def presigned_url(self, uri: str) -> str | None:
        """A time-limited URL the caller's browser can fetch directly,
        bypassing this process for the actual bytes.

        Not abstract: local disk has nothing to presign, so the default
        is None -- meaning "no shortcut, stream it yourself" -- and
        download_artifact (backend/api/routes/artifacts.py) falls back to
        its existing StreamingResponse path. A backend that can presign
        (S3Storage) overrides this; nothing about the local path or its
        tests changes.
        """
        return None

    @abstractmethod
    def open_stream(self, uri: str) -> BinaryIO:
        """Open the stored object for incremental reading.

        Separate from get() because the objects here are videos: reading a
        few hundred megabytes into memory to hand them to an HTTP response
        is fine exactly once and awful under any concurrency. Callers that
        genuinely want the whole thing in memory (a worker about to feed
        it to ffmpeg) keep using get().

        The caller owns closing the returned stream.

        Raises StorageObjectNotFoundError if the URI is unknown.
        """
