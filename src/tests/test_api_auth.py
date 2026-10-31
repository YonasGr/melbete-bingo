"""
Tests for the identity-resolution helpers in src.api.main:
- `_resolve_telegram_id`: admin auth (verified initData, or the
  token-gated fallback for a plain browser).
- `_require_player_identity`: player-scoped endpoint protection - a
  `tg_<id>` player_id must be backed by matching, verified initData.

These are pure functions (no Firestore access), so they're tested
directly without any fakes/mocks.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from src.api.main import _require_player_identity, _resolve_telegram_id
from src.core.config import settings


def _sign(pairs: dict, bot_token: str) -> str:
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": computed_hash})


@pytest.fixture(autouse=True)
def bot_token(monkeypatch):
    token = "123456:TESTTOKENfake"
    monkeypatch.setattr(settings, "telegram_bot_token", token)
    return token


def _init_data_for(user_id: int, bot_token: str) -> str:
    pairs = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "T"})}
    return _sign(pairs, bot_token)


# ---------------------------------------------------------------------
# _resolve_telegram_id (admin auth)
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_telegram_id_via_valid_init_data(bot_token):
    init_data = _init_data_for(42, bot_token)
    result = await _resolve_telegram_id(init_data)
    assert result == "42"


@pytest.mark.asyncio
async def test_resolve_telegram_id_rejects_invalid_init_data(bot_token):
    result = await _resolve_telegram_id("garbage-not-signed")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_telegram_id_rejects_tampered_data(bot_token):
    tampered = _init_data_for(42, bot_token).replace("42", "999")
    result = await _resolve_telegram_id(tampered)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_telegram_id_no_credentials_at_all(bot_token):
    assert await _resolve_telegram_id(None) is None


# ---------------------------------------------------------------------
# _require_player_identity (player-scoped endpoint protection)
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_require_player_identity_allows_matching_telegram_user(bot_token):
    init_data = _init_data_for(777, bot_token)
    await _require_player_identity("tg_777", init_data)  # must not raise


@pytest.mark.asyncio
async def test_require_player_identity_blocks_mismatched_id(bot_token):
    """The core spoofing scenario: someone knows/guesses another player's
    id and tries to act as them - must be rejected even with otherwise
    valid initData (just for a DIFFERENT Telegram user)."""
    init_data = _init_data_for(777, bot_token)
    with pytest.raises(HTTPException) as exc_info:
        await _require_player_identity("tg_999", init_data)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_player_identity_blocks_missing_init_data(bot_token):
    with pytest.raises(HTTPException) as exc_info:
        await _require_player_identity("tg_777", None)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_player_identity_blocks_web_only_ids(bot_token):
    with pytest.raises(HTTPException) as exc_info:
        await _require_player_identity("web_some-uuid", None)
    assert exc_info.value.status_code == 403
