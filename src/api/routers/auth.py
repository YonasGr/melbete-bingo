"""
Authentication, Link Codes, Player Preferences, and Profile endpoints.
"""
import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Header, HTTPException

from src.core.config import settings
from src.models.schemas import LinkCode
from src.api.dependencies import (
    _require_player_identity,
    get_link_codes,
    get_wallet_service,
)
from src.repositories import PlayerRepository

router = APIRouter(tags=["auth"])

LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LINK_CODE_TTL_MINUTES = 10


@router.post("/api/link/create")
async def create_link_code(
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    """Disabled during the security incident response."""
    raise HTTPException(status_code=410, detail="Browser account linking is disabled; open the app from Telegram")
    player_id = payload.get("player_id")
    if not player_id or not player_id.startswith("web_"):
        raise HTTPException(status_code=400, detail="Account linking is only for web sessions")
    await _require_player_identity(player_id, x_telegram_init_data)

    code = "".join(random.choices(LINK_CODE_ALPHABET, k=6))
    record = LinkCode(
        code=code,
        web_player_id=player_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=LINK_CODE_TTL_MINUTES),
    )
    await get_link_codes().create(record)

    bot_username = settings.telegram_bot_username.strip()
    deep_link = f"https://t.me/{bot_username}?start=link_{code}" if bot_username else None

    return {
        "code": code,
        "deep_link": deep_link,
        "expires_in_seconds": LINK_CODE_TTL_MINUTES * 60,
    }


@router.get("/api/link/status/{code}")
async def link_status(code: str):
    """Step 2: web page polling for account linking."""
    raise HTTPException(status_code=410, detail="Browser account linking is disabled; open the app from Telegram")
    record = await get_link_codes().get_by_code(code.strip().upper())
    if not record:
        raise HTTPException(status_code=404, detail="Unknown or expired link code")

    expires_at = record.expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if record.status == "pending" and expires_at and expires_at < datetime.now(timezone.utc):
        return {"status": "expired"}

    if record.status != "linked":
        return {"status": "pending"}

    return {
        "status": "linked",
        "player_id": record.linked_player_id,
        "session_token": record.session_token,
    }


@router.post("/api/player/{player_id}/auto-bingo")
async def set_auto_bingo(
    player_id: str,
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    enabled = bool(payload.get("enabled"))
    player_repo = PlayerRepository()
    player = await player_repo.get_by_id(player_id)
    if not player:
        return {"enabled": enabled, "persisted": False}
    await player_repo.set_auto_bingo(player_id, enabled)
    return {"enabled": enabled, "persisted": True}


@router.get("/api/player/{player_id}")
async def get_player_prefs(
    player_id: str,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    player = await PlayerRepository().get_by_id(player_id)
    if not player:
        return {"auto_bingo_enabled": False, "display_name": None, "registered": False}
    return {
        "auto_bingo_enabled": player.auto_bingo_enabled,
        "display_name": player.display_name,
        "registered": player.is_registered,
    }


@router.get("/api/profile/{player_id}")
async def get_profile(
    player_id: str,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    player = await PlayerRepository().get_by_id(player_id)
    wallet = await get_wallet_service().get_or_create_wallet(player_id)
    return {
        "player_id": player_id,
        "telegram_id": player.telegram_id if player else None,
        "display_name": player.display_name if player else None,
        "phone_number": player.phone_number if player else None,
        "registration_state": player.registration_state if player else "unregistered",
        "is_registered": player.is_registered if player else False,
        "balance": wallet.balance,
        "won_balance": wallet.won_balance,
        "bonus_balance": wallet.bonus_balance,
        "lifetime_deposited": wallet.lifetime_deposited,
        "referral_count": player.referral_count if player else 0,
        "referred_by": player.referred_by if player else None,
    }


@router.get("/api/referral-link/{player_id}")
async def get_referral_link(
    player_id: str,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    player = await PlayerRepository().get_by_id(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    from src.services.notifications import get_bot_username
    from src.services.referral_service import ReferralService

    bot_username = await get_bot_username()
    stats = await ReferralService().get_stats(player, bot_username)
    return stats
