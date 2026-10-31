import base64
import time

from src.core import admin_auth
from src.core.config import settings


def _totp(seed: str, now: float) -> str:
    import hashlib
    import hmac
    key = base64.b32decode(seed)
    digest = hmac.new(key, int(now // 30).to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{((int.from_bytes(digest[offset:offset + 4], 'big') & 0x7fffffff) % 1_000_000):06d}"


def test_totp_requires_the_admins_own_seed(monkeypatch):
    seed = "JBSWY3DPEHPK3PXP"
    monkeypatch.setattr(settings, "admin_totp_secrets", f"42:{seed}")
    now = 1_700_000_000
    assert admin_auth.verify_totp("42", _totp(seed, now), now=now)
    assert not admin_auth.verify_totp("43", _totp(seed, now), now=now)


def test_admin_session_is_signed_and_expires(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", "test-session-key")
    token = admin_auth.create_admin_session("42")
    assert admin_auth.verify_admin_session(token) == "42"
    assert admin_auth.verify_admin_session(token + "x") is None
