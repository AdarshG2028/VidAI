"""Shared HTTP streaming for a stored media object -- Range-aware, and
presigned-redirect-aware once a backend supports one.

Used by both artifact downloads (backend/api/routes/artifacts.py) and raw
video preview (backend/api/routes/projects.py's video download route),
which need byte-for-byte identical behavior once a caller is authorized
and a storage URI is known -- only *how* each gets there (a job's asset
list vs. a video's own storage_uri) differs. Factored out here specifically
so that difference doesn't turn into two Range-parsing implementations
that quietly drift apart.
"""

import mimetypes
import re
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse

from backend.storage import StorageObjectNotFoundError, get_storage

_CHUNK_SIZE = 64 * 1024
_FALLBACK_CONTENT_TYPE = "application/octet-stream"

# Only the single-range form. Browsers request one range at a time for
# media playback; multipart/byteranges exists but nothing that matters
# here emits it, and supporting it would mean generating MIME parts for
# no practical gain.
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Resolve a Range header to inclusive (start, end) byte offsets.

    Returns None when there is no range to honour, and raises 416 when one
    was asked for but cannot be satisfied -- the distinction matters,
    since a malformed range must not silently return the whole file.

    Handles all three forms: `bytes=500-999` (explicit), `bytes=500-`
    (open-ended, what a video element sends when it seeks), and
    `bytes=-500` (the final N bytes, used to read an MP4's trailing
    metadata when the index is not at the front).
    """
    if not header:
        return None

    match = _RANGE.match(header.strip())
    if not match or size <= 0:
        return None

    raw_start, raw_end = match.group(1), match.group(2)
    if not raw_start and not raw_end:
        return None

    if not raw_start:  # bytes=-N -> the last N bytes
        length = int(raw_end)
        if length <= 0:
            raise HTTPException(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                headers={"Content-Range": f"bytes */{size}"},
                detail="unsatisfiable range",
            )
        start, end = max(0, size - length), size - 1
    else:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
        end = min(end, size - 1)

    if start > end or start >= size:
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{size}"},
            detail="unsatisfiable range",
        )
    return start, end


def _stream(handle: BinaryIO, start: int, length: int):
    """Yield `length` bytes from `start`, in chunks.

    Closing is tied to the generator rather than the caller: the response
    body is produced lazily after the handler returns, so closing before
    that would shut the file before a single byte was sent.
    """
    try:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        handle.close()


async def stream_storage_object(
    request: Request, uri: str, *, fallback_filename: str = "artifact"
) -> StreamingResponse | RedirectResponse:
    """Stream a stored object, honouring Range requests -- or redirect to a
    presigned URL when the active backend can mint one.

    The caller is responsible for authorization; this function only knows
    that `uri` is safe to serve. Path traversal is handled by the storage
    backend itself (LocalDiskStorage._path_for / S3Storage._key_for reject
    any key containing a separator or a relative segment before it ever
    touches the filesystem or S3), not re-checked here.
    """
    storage = get_storage()

    redirect_url = storage.presigned_url(uri)
    if redirect_url is not None:
        return RedirectResponse(
            redirect_url,
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            # Every call mints a fresh, short-lived URL -- nothing here
            # should ever be cached and replayed past that window.
            headers={"Cache-Control": "no-store"},
        )

    try:
        size = storage.size(uri)
        handle = storage.open_stream(uri)
    except StorageObjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found"
        ) from exc

    try:
        window = _parse_range(request.headers.get("range"), size)
    except HTTPException:
        handle.close()
        raise

    filename = Path(uri).name or fallback_filename
    headers = {
        # Advertised so a client knows seeking is available at all; without
        # it browsers assume the whole file must be downloaded to play.
        "Accept-Ranges": "bytes",
        # inline, not attachment: this is meant to be playable in a video
        # element, and a browser download still works from the same URL.
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    content_type = mimetypes.guess_type(filename)[0] or _FALLBACK_CONTENT_TYPE

    if window is None:
        headers["Content-Length"] = str(size)
        return StreamingResponse(
            _stream(handle, 0, size), media_type=content_type, headers=headers
        )

    start, end = window
    length = end - start + 1
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(length)
    return StreamingResponse(
        _stream(handle, start, length),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=content_type,
        headers=headers,
    )
