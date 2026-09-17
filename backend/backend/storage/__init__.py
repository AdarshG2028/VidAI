"""Object/file storage abstractions for media artifacts."""

from functools import lru_cache

from backend.core.config import get_settings
from backend.storage.base import Storage, StorageObjectNotFoundError
from backend.storage.local import LocalDiskStorage

__all__ = ["Storage", "StorageObjectNotFoundError", "get_storage"]


@lru_cache
def get_storage() -> Storage:
    settings = get_settings()
    if settings.storage_backend == "s3":
        # Imported lazily so `local` (every test, every environment
        # without S3 credentials) never pays for importing boto3.
        from backend.storage.s3 import S3Storage

        return S3Storage(
            bucket=settings.storage_s3_bucket,
            region_name=settings.storage_s3_region,
            access_key_id=settings.storage_s3_access_key_id,
            secret_access_key=settings.storage_s3_secret_access_key,
            endpoint_url=settings.storage_s3_endpoint_url,
            presigned_url_ttl_seconds=settings.storage_s3_presigned_url_ttl_seconds,
        )
    return LocalDiskStorage(settings.storage_local_path)
