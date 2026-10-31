"""
Cloud Firestore bootstrap.

This module is the single place that knows how to authenticate against
Firebase and hand out a Firestore client. Every repository in
`src/repositories` gets its client from here.

We use `google.cloud.firestore.AsyncClient` (rather than the synchronous
client bundled with `firebase-admin`) so that Firestore calls don't block
FastAPI's event loop.

Credentials are resolved in this order:
1. `FIREBASE_CREDENTIALS_BASE64` - base64-encoded service-account JSON.
   Recommended for hosting platforms (Render, Railway, Fly.io, Cloud Run)
   where you'd rather paste a secret than upload a file.
2. `FIREBASE_CREDENTIALS_FILE` - path to a service-account JSON file.
   Convenient for local development (see README.md "Firebase setup").
3. Application Default Credentials - used automatically when running on
   Google Cloud infrastructure (Cloud Run, GKE, etc.) with no extra config.
"""
import base64
import json
import logging
from functools import lru_cache
from typing import Optional

from google.cloud.firestore import AsyncClient
from google.oauth2 import service_account

from src.core.config import settings

logger = logging.getLogger(__name__)


def _load_credentials() -> Optional[service_account.Credentials]:
    """Build service-account credentials from configured settings, or None
    to fall back to Application Default Credentials."""
    if settings.firebase_credentials_base64:
        raw = base64.b64decode(settings.firebase_credentials_base64)
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(info)

    if settings.firebase_credentials_file:
        return service_account.Credentials.from_service_account_file(
            settings.firebase_credentials_file
        )

    logger.info(
        "No explicit Firebase credentials configured; using Application Default Credentials"
    )
    return None


@lru_cache
def get_firestore_client() -> AsyncClient:
    """Return a cached, process-wide async Firestore client."""
    creds = _load_credentials()
    if creds is not None:
        return AsyncClient(project=settings.firebase_project_id, credentials=creds)
    return AsyncClient(project=settings.firebase_project_id)


async def init_firebase() -> None:
    """Eagerly verify Firestore connectivity on application startup so that
    configuration errors surface immediately instead of on first request."""
    client = get_firestore_client()
    # A cheap call that forces credential/network validation without
    # requiring any particular collection to exist yet.
    client.collection("_health").document("startup")
    logger.info("Firestore client ready for project '%s'", settings.firebase_project_id)
