"""Short-lived, signed admin sessions and RFC 6238 TOTP verification."""
import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from src.core.config import settings

ADMIN_SESSION_TTL_SECONDS = 15 * 60


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _totp_secret_for(telegram_id: str) -> Optional[str]:
    """Read an explicit id:base32-secret mapping; never accept a wildcard."""
    for entry in settings.admin_totp_secrets.split(","):
        admin_id, separator, secret = entry.strip().partition(":")
        if separator and hmac.compare_digest(admin_id, str(telegram_id)) and secret:
            return secret.replace(" ", "").upper()
    return None


def verify_totp(telegram_id: str, code: str, now: Optional[float] = None) -> bool:
    secret = _totp_secret_for(telegram_id)
    if not secret or not code or not code.isdigit() or len(code) != 6:
        return False
    try:
        key = base64.b32decode(secret, casefold=True)
    except Exception:  # malformed deployment configuration must fail closed
        return False
    counter = int((time.time() if now is None else now) // 30)
    for candidate in (counter - 1, counter, counter + 1):
        digest = hmac.new(key, candidate.to_bytes(8, "big"), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        value = (int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF) % 1_000_000
        if hmac.compare_digest(f"{value:06d}", code):
            return True
    return False


def create_admin_session(telegram_id: str) -> str:
    payload = json.dumps({"sub": str(telegram_id), "exp": int(time.time()) + ADMIN_SESSION_TTL_SECONDS}, separators=(",", ":")).encode()
    body = _b64encode(payload)
    signature = hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64encode(signature)}"


def verify_admin_session(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        body, signature = token.rsplit(".", 1)
        expected = hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(signature)):
            return None
        payload = json.loads(_b64decode(body))
        if int(payload["exp"]) < time.time() or not str(payload["sub"]).isdigit():
            return None
        return str(payload["sub"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError, base64.binascii.Error):
        return None
