"""
app/services/storage.py
-------------------------
Unified file storage client for driver verification documents
(national ID, license, vehicle registration).

Primary: local disk - zero cost, fine for a single-server deployment
Production: any S3-compatible object store (DigitalOcean Spaces, AWS S3, etc.)

Switching is one env var, mirroring the LLM_PROVIDER pattern used elsewhere
in this codebase:
  STORAGE_PROVIDER=local  -> writes to STORAGE_LOCAL_PATH on disk
  STORAGE_PROVIDER=s3     -> uploads to the configured S3-compatible bucket

Both providers return a storage "key" - an opaque string. DriverProfile's
document columns store this key, not a direct URL, since a private bucket
needs a freshly-signed URL generated on every read rather than a permanent link.
"""

import os
import uuid

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class StorageError(Exception):
    pass


class StorageService:

    def __init__(self):
        self.provider = settings.STORAGE_PROVIDER

    def _build_key(self, driver_user_id: str, doc_type: str, filename: str) -> str:
        ext = os.path.splitext(filename)[1].lower() or ".bin"
        return f"driver-documents/{driver_user_id}/{doc_type}_{uuid.uuid4().hex}{ext}"

    async def upload(
        self,
        driver_user_id: str,
        doc_type: str,
        filename: str,
        content: bytes,
    ) -> str:
        """Stores the file and returns its storage key."""
        key = self._build_key(driver_user_id, doc_type, filename)

        if self.provider == "local":
            await self._upload_local(key, content)
        elif self.provider == "s3":
            await self._upload_s3(key, content)
        else:
            raise StorageError(f"Unknown storage provider: {self.provider}")

        logger.info("document_uploaded", driver_user_id=driver_user_id, doc_type=doc_type, key=key)
        return key

    async def get_url(self, key: str, expires_seconds: int = 3600) -> str:
        """
        Returns a URL the admin dashboard can use to view the document.
        Local mode returns a path served by the (to-be-added) admin document
        route; S3 mode returns a time-limited presigned URL.
        """
        if self.provider == "local":
            return f"{settings.APP_BASE_URL}/api/v1/admin/documents/{key}"
        elif self.provider == "s3":
            return await self._presigned_url_s3(key, expires_seconds)
        raise StorageError(f"Unknown storage provider: {self.provider}")

    # ── Local disk ────────────────────────────────────────────────────────────

    async def _upload_local(self, key: str, content: bytes) -> None:
        import asyncio

        path = os.path.join(settings.STORAGE_LOCAL_PATH, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        def _write():
            with open(path, "wb") as f:
                f.write(content)

        await asyncio.get_event_loop().run_in_executor(None, _write)

    def read_local(self, key: str) -> bytes:
        """Used by a local-mode document-serving route. Raises if the key doesn't exist."""
        path = os.path.join(settings.STORAGE_LOCAL_PATH, key)
        with open(path, "rb") as f:
            return f.read()

    # ── S3-compatible (DigitalOcean Spaces / AWS S3) ────────────────────────────

    def _s3_client(self):
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=settings.STORAGE_S3_ENDPOINT_URL or None,
            aws_access_key_id=settings.STORAGE_S3_ACCESS_KEY,
            aws_secret_access_key=settings.STORAGE_S3_SECRET_KEY,
            region_name=settings.STORAGE_S3_REGION,
        )

    async def _upload_s3(self, key: str, content: bytes) -> None:
        import asyncio

        client = self._s3_client()

        def _put():
            client.put_object(
                Bucket=settings.STORAGE_S3_BUCKET,
                Key=key,
                Body=content,
                ACL="private",
            )

        await asyncio.get_event_loop().run_in_executor(None, _put)

    async def _presigned_url_s3(self, key: str, expires_seconds: int) -> str:
        import asyncio

        client = self._s3_client()

        def _sign():
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.STORAGE_S3_BUCKET, "Key": key},
                ExpiresIn=expires_seconds,
            )

        return await asyncio.get_event_loop().run_in_executor(None, _sign)


# ── Singleton ──────────────────────────────────────────────────────────────────
storage_service = StorageService()
