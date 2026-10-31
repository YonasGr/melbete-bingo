"""In-app notification inbox: list, unread count, mark read. A player can only ever see and change
their own notifications (identity is verified from Telegram initData, like the wallet routes)."""
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query

from src.api.dependencies import _require_player_identity
from src.core.rate_limit import enforce as enforce_rate_limit
from src.repositories.notification_repository import NotificationRepository
from src.repositories.player_repository import PlayerRepository

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_repo: Optional[NotificationRepository] = None


def _get_repo() -> NotificationRepository:
    global _repo
    if _repo is None:
        _repo = NotificationRepository()
    return _repo


def _public(row: dict) -> dict:
    data = row.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    return {
        "id": row["id"],
        "type": row["type"],
        "title": row["title"],
        "body": row["body"],
        "data": data or {},
        "read": row["read_at"] is not None,
        "created_at": row["created_at"].isoformat(),
    }


@router.get("")
async def list_notifications(
    player_id: str = Query(...),
    limit: int = Query(30, ge=1, le=100),
    before: Optional[str] = Query(None, description="ISO timestamp: only notifications older than this"),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    before_dt = None
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
        except ValueError:
            raise HTTPException(status_code=400, detail="before must be an ISO timestamp")
    repo = _get_repo()
    rows = await repo.list_for_player(player_id, limit, before_dt)
    return {"notifications": [_public(r) for r in rows], "unread_count": await repo.unread_count(player_id)}


@router.get("/unread-count")
async def unread_count(
    player_id: str = Query(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    return {"unread_count": await _get_repo().unread_count(player_id)}


@router.post("/read")
async def mark_read(
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    """Body: {"player_id": "...", "ids": ["..."]}  - omit "ids" to mark everything read."""
    player_id = payload.get("player_id")
    if not player_id:
        raise HTTPException(status_code=400, detail="player_id is required")
    await _require_player_identity(player_id, x_telegram_init_data)
    ids = payload.get("ids")
    if ids is not None and (not isinstance(ids, list) or len(ids) > 200 or not all(isinstance(i, str) for i in ids)):
        raise HTTPException(status_code=400, detail="ids must be a list of up to 200 notification ids")
    repo = _get_repo()
    changed = await repo.mark_read(player_id, ids)
    return {"changed": changed, "unread_count": await repo.unread_count(player_id)}


@router.get("/preferences")
async def get_preferences(
    player_id: str = Query(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    player = await PlayerRepository().get_by_id(player_id)
    return {"promos": not bool(getattr(player, "promo_opt_out", False))}


@router.post("/preferences")
async def set_preferences(
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    """Body: {"player_id": "...", "promos": true|false}. Promotional messages are nudges, streak
    reminders and win-back offers. Messages about the player's own money are always delivered."""
    player_id = payload.get("player_id")
    promos = payload.get("promos")
    if not player_id or not isinstance(promos, bool):
        raise HTTPException(status_code=400, detail="player_id and a true/false promos are required")
    await _require_player_identity(player_id, x_telegram_init_data)
    enforce_rate_limit("notify_prefs", player_id)
    await PlayerRepository().update(player_id, {"promo_opt_out": not promos})
    return {"promos": promos}
