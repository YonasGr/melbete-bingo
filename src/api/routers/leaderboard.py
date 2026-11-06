"""Leaderboard campaigns: player view + channel reward claim, and the admin campaign controls."""
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request

from src.api.dependencies import _reject_if_banned, _require_player_identity, get_control_service, require_super_admin_dep
from src.core.rate_limit import client_ip
from src.services.leaderboard_service import LeaderboardError, get_leaderboard_service

router = APIRouter(tags=["leaderboard"])


def _http(e: LeaderboardError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.message)


async def _audit(request: Request, actor: str, action: str, **kw) -> None:
    await get_control_service().audit(actor, action, ip=client_ip(request), **kw)


# ----------------------------------------------------------------------------- players
@router.get("/api/leaderboard/{player_id}")
async def player_leaderboard(player_id: str, x_telegram_init_data: Optional[str] = Header(default=None, alias="X-Telegram-Init-Data")):
    await _require_player_identity(player_id, x_telegram_init_data)
    return await get_leaderboard_service().player_view(player_id)


@router.post("/api/leaderboard/{player_id}/channel-claim")
async def claim_channel_reward(player_id: str, x_telegram_init_data: Optional[str] = Header(default=None, alias="X-Telegram-Init-Data")):
    """Server-side verified: the bot asks Telegram whether this exact user is in the admin's channel."""
    await _require_player_identity(player_id, x_telegram_init_data)
    await _reject_if_banned(player_id)
    try:
        return await get_leaderboard_service().claim_channel(player_id)
    except LeaderboardError as e:
        raise _http(e)


# ----------------------------------------------------------------------------- admin
@router.get("/api/admin/leaderboard/campaigns")
async def admin_campaigns(requester: str = Depends(require_super_admin_dep)):
    return {"campaigns": await get_leaderboard_service().admin_list()}


@router.post("/api/admin/leaderboard/campaigns")
async def admin_create_campaign(request: Request, payload: dict = Body(...), requester: str = Depends(require_super_admin_dep)):
    try:
        c = await get_leaderboard_service().create(requester, payload)
    except LeaderboardError as e:
        raise _http(e)
    await _audit(request, requester, "leaderboard.create", target_type="leaderboard", target_id=c["id"], summary=f"Created campaign {c['name']}")
    return {"campaign": c}


@router.put("/api/admin/leaderboard/campaigns/{campaign_id}")
async def admin_update_campaign(campaign_id: str, request: Request, payload: dict = Body(...), requester: str = Depends(require_super_admin_dep)):
    try:
        c = await get_leaderboard_service().update(requester, campaign_id, payload)
    except LeaderboardError as e:
        raise _http(e)
    await _audit(request, requester, "leaderboard.update", target_type="leaderboard", target_id=campaign_id, summary=f"Updated campaign {c['name']}", after=payload)
    return {"campaign": c}


@router.get("/api/admin/leaderboard/campaigns/{campaign_id}")
async def admin_campaign_detail(campaign_id: str, requester: str = Depends(require_super_admin_dep)):
    try:
        return await get_leaderboard_service().admin_detail(campaign_id)
    except LeaderboardError as e:
        raise _http(e)


@router.post("/api/admin/leaderboard/campaigns/{campaign_id}/extend")
async def admin_extend_campaign(campaign_id: str, request: Request, payload: dict = Body(...), requester: str = Depends(require_super_admin_dep)):
    """Body: {"ends_at": "2026-12-05T21:00"} in the campaign's timezone. Later than the current end only."""
    service = get_leaderboard_service()
    try:
        before = await service.admin_detail_brief(campaign_id)
        c = await service.extend(requester, campaign_id, payload.get("ends_at"))
    except LeaderboardError as e:
        raise _http(e)
    await _audit(request, requester, "leaderboard.extend", target_type="leaderboard", target_id=campaign_id,
                 summary=f"Extended {c['name']} to {c['ends_local']} ({c['timezone']})",
                 before={"ends_at": before["ends_at"]}, after={"ends_at": c["ends_at"]})
    return {"campaign": c}


@router.post("/api/admin/leaderboard/campaigns/{campaign_id}/cancel")
async def admin_cancel_campaign(campaign_id: str, request: Request, requester: str = Depends(require_super_admin_dep)):
    try:
        c = await get_leaderboard_service().cancel(requester, campaign_id)
    except LeaderboardError as e:
        raise _http(e)
    await _audit(request, requester, "leaderboard.cancel", target_type="leaderboard", target_id=campaign_id, summary=f"Cancelled {c['name']}")
    return {"campaign": c}


@router.post("/api/admin/leaderboard/campaigns/{campaign_id}/approve")
async def admin_approve_distribution(campaign_id: str, request: Request, payload: dict = Body(default={}), requester: str = Depends(require_super_admin_dep)):
    try:
        res = await get_leaderboard_service().approve(requester, campaign_id, (payload or {}).get("note", ""))
    except LeaderboardError as e:
        raise _http(e)
    await _audit(request, requester, "leaderboard.approve", target_type="leaderboard", target_id=campaign_id,
                 summary=f"Approved distribution: {res['total_distributed']} ETB to {res['winners']} winners", after=res)
    return res


@router.post("/api/admin/leaderboard/campaigns/{campaign_id}/reject")
async def admin_reject_distribution(campaign_id: str, request: Request, payload: dict = Body(default={}), requester: str = Depends(require_super_admin_dep)):
    try:
        c = await get_leaderboard_service().reject(requester, campaign_id, (payload or {}).get("note", ""))
    except LeaderboardError as e:
        raise _http(e)
    await _audit(request, requester, "leaderboard.reject", target_type="leaderboard", target_id=campaign_id,
                 summary="Rejected distribution", after={"note": (payload or {}).get("note", "")})
    return {"campaign": c}


@router.post("/api/admin/leaderboard/points/{points_id}/revoke")
async def admin_revoke_points(points_id: str, request: Request, requester: str = Depends(require_super_admin_dep)):
    try:
        await get_leaderboard_service().revoke_points(requester, points_id)
    except LeaderboardError as e:
        raise _http(e)
    await _audit(request, requester, "leaderboard.revoke", target_type="leaderboard_points", target_id=points_id, summary="Revoked channel claim")
    return {"ok": True}


@router.get("/api/admin/attention")
async def admin_attention(requester: str = Depends(require_super_admin_dep)):
    """What is waiting for a decision: shown as red counts on the Leaderboard and Agents tabs."""
    from src.models.schemas import utcnow
    from src.repositories.agent_repository import AgentRepository

    return {
        "leaderboard": await get_leaderboard_service().repo.awaiting_decision_count(utcnow()),
        "agents": await AgentRepository().pending_application_count(),
    }
