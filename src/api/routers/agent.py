"""Agent (reseller) self-service API: dashboard, player top-ups, commission cash-out.

Every call is tied to a verified Telegram identity (same check as wallet/bingo routes) and to the
agent account of that player. Money moves only through AgentService -> atomic repository methods.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query

from src.api.dependencies import _reject_if_banned, _require_player_identity, get_agent_service, get_control_service
from src.api.idempotency import run_idempotent
from src.core.rate_limit import enforce as enforce_rate_limit
from src.services.agent_service import (
    AgentAlreadyExistsError,
    AgentError,
    AgentNotFoundError,
    AgentSuspendedError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])


def _http(e: AgentError) -> HTTPException:
    if isinstance(e, AgentNotFoundError):
        return HTTPException(status_code=404, detail=e.message)
    if isinstance(e, (AgentSuspendedError,)):
        return HTTPException(status_code=403, detail=e.message)
    if isinstance(e, AgentAlreadyExistsError):
        return HTTPException(status_code=409, detail=e.message)
    return HTTPException(status_code=400, detail=e.message)


async def _auth(player_id: str, init_data: Optional[str]) -> None:
    await _require_player_identity(player_id, init_data)
    await _reject_if_banned(player_id)
    blocked = await get_control_service().flag_block("agent_portal")
    if blocked:
        raise HTTPException(status_code=503, detail=blocked)


async def _agent_link(agent_code: str) -> str:
    try:
        from src.services.notifications import get_bot_username

        username = await get_bot_username()
    except Exception:  # noqa: BLE001 - the link is a convenience
        username = ""
    return f"https://t.me/{username}?start=ag_{agent_code}" if username else ""


@router.get("/me")
async def agent_me(
    player_id: str = Query(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _auth(player_id, x_telegram_init_data)
    try:
        d = await get_agent_service().dashboard(player_id)
    except AgentError as e:
        if isinstance(e, AgentNotFoundError):
            return {"is_agent": False}
        raise _http(e)
    agent = d["agent"]
    try:
        from src.services.agent_rewards import get_agent_reward_service

        rewards = await get_agent_reward_service().agent_summary(agent)
    except Exception:  # noqa: BLE001 - the dashboard must still load if rewards are unavailable
        logger.exception("Could not load agent rewards summary")
        rewards = None
    return {
        "is_agent": True,
        "agent": agent.model_dump(mode="json"),
        "commission_percent": (max(agent.commission_bps, agent.tier_bps) / 100),
        "rewards": rewards,
        "player_count": d["player_count"],
        "link": await _agent_link(agent.agent_code),
        "ledger": [e.model_dump(mode="json") for e in d["ledger"]],
    }


@router.get("/players")
async def agent_players(
    player_id: str = Query(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _auth(player_id, x_telegram_init_data)
    try:
        rows = await get_agent_service().list_players(player_id)
    except AgentError as e:
        raise _http(e)
    return {"players": [
        {"telegram_id": r["telegram_id"], "display_name": r["display_name"], "joined": r["created_at"].isoformat()}
        for r in rows
    ]}


@router.post("/topup")
async def agent_topup(
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Body: {"player_id": "<agent's own id>", "telegram_id": "<target>", "amount": 500}"""
    player_id = payload.get("player_id")
    if not player_id:
        raise HTTPException(status_code=400, detail="player_id is required")
    await _auth(player_id, x_telegram_init_data)
    enforce_rate_limit("agent_topup", player_id)

    async def _apply() -> dict:
        try:
            r = await get_agent_service().topup_player(player_id, payload.get("telegram_id"), payload.get("amount"))
        except AgentError as e:
            raise _http(e)
        return {
            "success": True,
            "player_name": r["player_name"],
            "amount": payload.get("amount"),
            "commission": r["commission"],
            "float_balance": r["agent"].float_balance,
            "commission_balance": r["agent"].commission_balance,
            "transaction_id": r["transaction_id"],
        }

    return await run_idempotent(player_id, "agent_topup", idempotency_key, payload, _apply)


@router.post("/commission/cashout")
async def agent_cashout(
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Body: {"player_id": "<agent's own id>", "amount": 300}. Moves commission into the agent's
    wallet as withdrawable balance; it then leaves through the normal withdrawal approval flow."""
    player_id = payload.get("player_id")
    if not player_id:
        raise HTTPException(status_code=400, detail="player_id is required")
    await _auth(player_id, x_telegram_init_data)
    enforce_rate_limit("agent_cashout", player_id)

    async def _apply() -> dict:
        try:
            r = await get_agent_service().cashout_commission(player_id, payload.get("amount"))
        except AgentError as e:
            raise _http(e)
        return {
            "success": True,
            "amount": payload.get("amount"),
            "commission_balance": r["agent"].commission_balance,
            "wallet_balance": r["wallet_balance"],
            "transaction_id": r["transaction_id"],
        }

    return await run_idempotent(player_id, "agent_cashout", idempotency_key, payload, _apply)
