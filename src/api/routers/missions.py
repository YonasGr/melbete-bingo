"""Daily missions for the Promotions screen."""
from fastapi import APIRouter, Body, Header, HTTPException

from src.api.dependencies import _reject_if_banned, _require_player_identity
from src.services.missions import MissionError, get_mission_service

router = APIRouter(tags=["missions"])


@router.get("/api/missions/{player_id}")
async def missions(player_id: str, x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    await _require_player_identity(player_id, x_telegram_init_data)
    return await get_mission_service().summary(player_id)


@router.post("/api/missions/{player_id}/claim")
async def claim_mission(player_id: str, payload: dict = Body(...), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    await _require_player_identity(player_id, x_telegram_init_data)
    await _reject_if_banned(player_id)
    try:
        rounds = int(payload.get("rounds"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="rounds must be a number")
    try:
        return await get_mission_service().claim(player_id, rounds)
    except MissionError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
