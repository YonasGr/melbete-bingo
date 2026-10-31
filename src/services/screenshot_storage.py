import logging
import os
from pathlib import Path
from typing import Optional
from src.core.config import settings

logger = logging.getLogger(__name__)

UPLOADS_DIR = Path("uploads/deposits")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

def save_screenshot(filename: str, content: bytes) -> None:
    """Saves a screenshot to either Azure Blob Storage (if configured) or local disk."""
    if settings.azure_storage_connection_string:
        try:
            from azure.storage.blob import BlobServiceClient
            blob_service_client = BlobServiceClient.from_connection_string(settings.azure_storage_connection_string)
            container_client = blob_service_client.get_container_client(settings.azure_storage_container_name)
            try:
                container_client.create_container()
            except Exception:
                pass
            blob_client = container_client.get_blob_client(filename)
            blob_client.upload_blob(content, overwrite=True)
            logger.info("Successfully uploaded screenshot %s to Azure Blob Storage container %s", filename, settings.azure_storage_container_name)
            return
        except Exception:
            logger.exception("Failed to upload screenshot %s to Azure Blob Storage, falling back to local disk", filename)

    # Fallback to local disk
    with open(UPLOADS_DIR / filename, "wb") as f:
        f.write(content)
    logger.info("Saved screenshot %s to local disk", filename)


def get_screenshot(filename: str) -> tuple[Optional[bytes], Optional[str]]:
    """Retrieves screenshot bytes and its MIME type.
    First tries Azure Blob Storage (if configured), then falls back to local disk."""
    ext = filename.split(".")[-1].lower()
    mime_types = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    mime = mime_types.get(ext, "image/jpeg")

    if settings.azure_storage_connection_string:
        try:
            from azure.storage.blob import BlobServiceClient
            blob_service_client = BlobServiceClient.from_connection_string(settings.azure_storage_connection_string)
            blob_client = blob_service_client.get_blob_client(container=settings.azure_storage_container_name, blob=filename)
            if blob_client.exists():
                stream = blob_client.download_blob()
                content = stream.readall()
                logger.info("Successfully retrieved screenshot %s from Azure Blob Storage", filename)
                return content, mime
        except Exception:
            logger.exception("Failed to retrieve screenshot %s from Azure Blob Storage, checking local disk", filename)

    # Fallback to local disk
    path = UPLOADS_DIR / filename
    if path.is_file():
        try:
            with open(path, "rb") as f:
                content = f.read()
            logger.info("Successfully retrieved screenshot %s from local disk", filename)
            return content, mime
        except Exception:
            logger.exception("Failed to read screenshot %s from local disk", filename)

    return None, None
