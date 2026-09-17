"""S3-backed Storage. Same key shape and key-safety guard as
LocalDiskStorage (backend/storage/base.py) -- the two backends differ only
in where the bytes live, not in how a URI is validated.

boto3 rather than an AWS-specific SDK, and endpoint_url left overridable,
so the same class can point at Cloudflare R2, Backblaze B2 or MinIO later
with a settings change and zero code change -- the reason this backend was
designed generically even though the current target is plain AWS S3.
"""

from typing import BinaryIO

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from backend.storage.base import Storage, StorageObjectNotFoundError, generate_key, is_safe_key

_SCHEME = "s3://"


class S3Storage(Storage):
    def __init__(
        self,
        *,
        bucket: str,
        region_name: str,
        access_key_id: str,
        secret_access_key: str,
        endpoint_url: str | None = None,
        presigned_url_ttl_seconds: int = 3600,
    ) -> None:
        self._bucket = bucket
        self._presigned_url_ttl_seconds = presigned_url_ttl_seconds
        self._client = boto3.client(
            "s3",
            region_name=region_name,
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            # v4 is AWS's current default anyway; required (not just
            # preferred) by R2 and most S3-compatible providers, so set it
            # unconditionally rather than only when endpoint_url is set.
            config=Config(signature_version="s3v4"),
        )

    def put(self, data: bytes, *, suggested_name: str | None = None) -> str:
        key = generate_key(suggested_name)
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
        return f"{_SCHEME}{key}"

    def put_file(self, path, *, suggested_name: str | None = None) -> str:
        key = generate_key(suggested_name or str(path))
        # upload_file handles multipart transfer for large files itself --
        # the whole reason put_file exists separately from put(), per
        # backend/storage/base.py's docstring.
        self._client.upload_file(str(path), self._bucket, key)
        return f"{_SCHEME}{key}"

    def get(self, uri: str) -> bytes:
        key = self._key_for(uri)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise self._not_found_or_reraise(uri, exc)
        return response["Body"].read()

    def delete(self, uri: str) -> bool:
        key = self._key_for(uri, allow_missing=True)
        if key is None or not self.exists(uri):
            return False
        self._client.delete_object(Bucket=self._bucket, Key=key)
        return True

    def exists(self, uri: str) -> bool:
        key = self._key_for(uri, allow_missing=True)
        if key is None:
            return False
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError:
            return False
        return True

    def size(self, uri: str) -> int:
        key = self._key_for(uri)
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise self._not_found_or_reraise(uri, exc)
        return response["ContentLength"]

    def open_stream(self, uri: str) -> BinaryIO:
        key = self._key_for(uri)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise self._not_found_or_reraise(uri, exc)
        # StreamingBody satisfies read()/close(), which is all callers of
        # open_stream (backend/api/routes/artifacts.py) actually use --
        # but in practice, presigned_url() below means download_artifact
        # never calls this for an S3 backend at all; it exists for
        # interface completeness and any caller that wants get()-like
        # behaviour without the redirect (e.g. a worker reading its input).
        return response["Body"]

    def presigned_url(self, uri: str) -> str | None:
        key = self._key_for(uri, allow_missing=True)
        if key is None:
            return None
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=self._presigned_url_ttl_seconds,
        )

    def _key_for(self, uri: str, *, allow_missing: bool = False) -> str | None:
        """Mirrors LocalDiskStorage._path_for: resolves a URI to a key, or
        None if it can't possibly be one this backend wrote. Raises
        StorageObjectNotFoundError instead of returning None when the
        caller has no other way to signal "not found" (get/size/
        open_stream, whose return types don't include None)."""
        if not uri.startswith(_SCHEME):
            if allow_missing:
                return None
            raise StorageObjectNotFoundError(uri)
        key = uri.removeprefix(_SCHEME)
        if not is_safe_key(key):
            if allow_missing:
                return None
            raise StorageObjectNotFoundError(uri)
        return key

    @staticmethod
    def _not_found_or_reraise(uri: str, exc: ClientError) -> Exception:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            return StorageObjectNotFoundError(uri)
        return exc
