"""Local-disk Storage backend — the only one V1 needs, since the API and
workers all run on the same host via uv (see README). A real deployment
would swap in an S3-compatible backend behind the same Storage interface;
nothing outside this module would change.
"""

import shutil
from pathlib import Path
from typing import BinaryIO

from backend.storage.base import Storage, StorageObjectNotFoundError, generate_key, is_safe_key

_SCHEME = "local://"
_COPY_CHUNK_SIZE = 1024 * 1024


class LocalDiskStorage(Storage):
    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, *, suggested_name: str | None = None) -> str:
        key = generate_key(suggested_name)
        (self._base_dir / key).write_bytes(data)
        return f"{_SCHEME}{key}"

    def put_file(self, path, *, suggested_name: str | None = None) -> str:
        key = generate_key(suggested_name or str(path))
        destination = self._base_dir / key
        # Copied rather than moved: the caller owns its temp file and
        # deletes it in a finally block, and a move would pull the ground
        # out from under that.
        with open(path, "rb") as source, open(destination, "wb") as target:
            shutil.copyfileobj(source, target, _COPY_CHUNK_SIZE)
        return f"{_SCHEME}{key}"

    def get(self, uri: str) -> bytes:
        path = self._path_for(uri)
        if path is None or not path.is_file():
            raise StorageObjectNotFoundError(uri)
        return path.read_bytes()

    def delete(self, uri: str) -> bool:
        path = self._path_for(uri)
        if path is None or not path.is_file():
            return False
        path.unlink(missing_ok=True)
        return True

    def exists(self, uri: str) -> bool:
        path = self._path_for(uri)
        return path is not None and path.is_file()

    def size(self, uri: str) -> int:
        path = self._path_for(uri)
        if path is None or not path.is_file():
            raise StorageObjectNotFoundError(uri)
        return path.stat().st_size

    def open_stream(self, uri: str) -> BinaryIO:
        path = self._path_for(uri)
        if path is None or not path.is_file():
            raise StorageObjectNotFoundError(uri)
        return path.open("rb")

    def _path_for(self, uri: str) -> Path | None:
        """Resolves a URI to a filesystem path, or None if it can't possibly
        be one this backend wrote — not the same as "not found", callers
        still need to check the filesystem for that.
        """
        if not uri.startswith(_SCHEME):
            return None
        key = uri.removeprefix(_SCHEME)
        # A key is always one generate_key() produced -- reject anything
        # else outright rather than letting a garbled/malicious URI resolve
        # to a path outside base_dir.
        if not is_safe_key(key):
            return None
        return self._base_dir / key
