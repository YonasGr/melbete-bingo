"""
Tests for src.core.telegram_auth - the HMAC-SHA256 verification of
Telegram WebApp `initData`. This is the ONLY trustworthy way the backend
establishes a caller's real Telegram identity, so its correctness is
security-critical: a bug here either locks out legitimate users or (far
worse) lets a forged identity through.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from src.core import telegram_auth
from src.core.config import settings


def _sign(pairs: dict, bot_token: str) -> str:
    """Builds a valid, correctly-signed initData string exactly the way
    Telegram's client does, so tests exercise the real verification path."""
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    full = {**pairs, "hash": computed_hash}
    return urlencode(full)


@pytest.fixture(autouse=True)
def bot_token(monkeypatch):
    token = "123456:TESTTOKENfake"
    monkeypatch.setattr(settings, "telegram_bot_token", token)
    return token


def _valid_pairs(user_id=987654321):
    return {
        "auth_date": str(int(time.time())),
        "query_id": "AAABBBCCC",
        "user": json.dumps({"id": user_id, "first_name": "Test", "username": "testuser"}),
    }


def test_valid_init_data_verifies(bot_token):
    init_data = _sign(_valid_pairs(), bot_token)
    result = telegram_auth.verify_init_data(init_data)
    assert result is not None
    assert result["user"]["id"] == 987654321


def test_extract_telegram_id_happy_path(bot_token):
    init_data = _sign(_valid_pairs(user_id=555), bot_token)
    assert telegram_auth.extract_telegram_id(init_data) == "555"


def test_tampered_user_id_fails_verification(bot_token):
    """The whole point of the signature: changing so much as one field
    after signing must invalidate it."""
    pairs = _valid_pairs(user_id=111)
    init_data = _sign(pairs, bot_token)
    # Attacker swaps their own id in after the fact, without the secret
    # key needed to produce a matching hash.
    forged = init_data.replace("111", "999")
    assert telegram_auth.verify_init_data(forged) is None


def test_wrong_bot_token_fails_verification(bot_token):
    """Signed with a DIFFERENT bot's token - must never verify against
    ours, or any bot could forge identities for this one."""
    init_data = _sign(_valid_pairs(), "different-bot-token")
    assert telegram_auth.verify_init_data(init_data) is None


def test_missing_hash_fails(bot_token):
    pairs = _valid_pairs()
    init_data = urlencode(pairs)  # no hash included at all
    assert telegram_auth.verify_init_data(init_data) is None


def test_expired_auth_date_fails(bot_token):
    pairs = _valid_pairs()
    pairs["auth_date"] = str(int(time.time()) - 999999)  # ~11 days old
    init_data = _sign(pairs, bot_token)
    assert telegram_auth.verify_init_data(init_data, max_age_seconds=86400) is None


def test_empty_init_data_returns_none(bot_token):
    assert telegram_auth.verify_init_data("") is None
    assert telegram_auth.extract_telegram_id(None) is None
    assert telegram_auth.extract_telegram_id("") is None


def test_no_bot_token_configured_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    init_data = _sign(_valid_pairs(), "some-token")
    assert telegram_auth.verify_init_data(init_data) is None
