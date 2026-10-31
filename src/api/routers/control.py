"""Admin Control Centre API: draft/publish config, emergency switches, audit log, player controls.

Admin endpoints live under /api/admin (cookie session, role-based permissions); the single public
endpoint, GET /api/control/public, is what the player app polls for maintenance, flags and content.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from datetime import timedelta

from src.api.dependencies import (
    get_admin_service, get_control_service, get_room_service, manager, require_admin_dep, require_perm,
)
from src.core.config import settings as env_settings
from src.repositories.admin_repository import AdminRepository
from src.repositories.live_repository import LiveStatsRepository
from src.services import live_logic
from src.services.admin_alerts import get_alert_service
from src.core.rate_limit import client_ip
from src.models.schemas import Transaction
from src.repositories import PlayerRepository
from src.repositories.transaction_repository import TransactionRepository
from src.repositories.wallet_repository import WalletRepository
from src.services import control_logic as cl
from src.services.control_service import ControlError, ControlPermissionError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["control"])


def _raise(e: Exception):
    if isinstance(e, ControlPermissionError):
        raise HTTPException(status_code=403, detail=str(e))
    raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Public: what the player app is allowed to know
# ---------------------------------------------------------------------------

@router.get("/api/control/public")
async def control_public(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await get_control_service().public()


# ---------------------------------------------------------------------------
# Draft / publish
# ---------------------------------------------------------------------------

@router.get("/api/admin/control")
async def control_get(admin_id: str = Depends(require_admin_dep)):
    return await get_control_service().snapshot(admin_id)


@router.put("/api/admin/control/draft")
async def control_save_draft(request: Request, payload: dict = Body(...), admin_id: str = Depends(require_admin_dep)):
    try:
        return await get_control_service().save_draft(admin_id, payload.get("config") or {}, ip=client_ip(request))
    except ControlError as e:
        _raise(e)


@router.post("/api/admin/control/discard")
async def control_discard(request: Request, admin_id: str = Depends(require_admin_dep)):
    try:
        return await get_control_service().discard_draft(admin_id, ip=client_ip(request))
    except ControlError as e:
        _raise(e)


@router.post("/api/admin/control/publish")
async def control_publish(request: Request, admin_id: str = Depends(require_admin_dep)):
    try:
        return await get_control_service().publish(admin_id, ip=client_ip(request))
    except ControlError as e:
        _raise(e)


@router.post("/api/admin/control/rollback")
async def control_rollback(request: Request, admin_id: str = Depends(require_admin_dep)):
    try:
        return await get_control_service().rollback(admin_id, ip=client_ip(request))
    except ControlError as e:
        _raise(e)


@router.post("/api/admin/control/emergency")
async def control_emergency(request: Request, payload: dict = Body(...), admin_id: str = Depends(require_admin_dep)):
    """Maintenance mode and the kill switch apply immediately - they are not part of drafts."""
    maintenance = payload.get("maintenance")
    kill_switch = payload.get("kill_switch")
    if maintenance is None and kill_switch is None:
        raise HTTPException(status_code=400, detail="Nothing to change")
    try:
        return await get_control_service().set_emergency(
            admin_id, maintenance=maintenance, kill_switch=kill_switch, ip=client_ip(request),
        )
    except ControlError as e:
        _raise(e)


@router.get("/api/admin/roles")
async def control_roles(admin_id: str = Depends(require_admin_dep)):
    """Permission catalogue + role presets, so the staff UI can render them."""
    return {
        "permissions": cl.PERMISSIONS,
        "roles": {
            r: {"label": cl.ROLE_LABELS[r], "permissions": sorted(cl.ROLE_PRESETS[r])}
            for r in (*cl.ASSIGNABLE_ROLES, "super")
        },
        "assignable": list(cl.ASSIGNABLE_ROLES),
    }


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

@router.get("/api/admin/audit")
async def audit_list(
    limit: int = 100,
    before_id: Optional[int] = None,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    admin_id: str = Depends(require_perm("audit.view")),
):
    rows = await get_control_service().audit_repo.list(
        limit=limit, before_id=before_id, actor_id=actor, action_prefix=action,
        target_type=target_type, target_id=target_id,
    )
    entries = [{**r, "created_at": r["created_at"].isoformat()} for r in rows]
    return {"entries": entries, "next_before_id": entries[-1]["id"] if len(entries) >= max(1, min(limit, 500)) else None}


# ---------------------------------------------------------------------------
# Player controls
# ---------------------------------------------------------------------------

async def _player_or_404(player_id: str):
    player = await PlayerRepository().get_by_id(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    return player


@router.get("/api/admin/users/{player_id}/control")
async def player_control_detail(player_id: str, admin_id: str = Depends(require_perm("players.view"))):
    player = await _player_or_404(player_id)
    control = get_control_service()
    wallet = await WalletRepository().get_by_player(player_id)
    notes = await control.player_repo.list_notes(player_id)
    txns = await TransactionRepository().list_by_player(player_id, limit=40)
    trail = await control.audit_repo.list(limit=40, target_type="player", target_id=player_id)
    cfg = await control.get_published()
    return {
        "player": {
            "id": player.id, "telegram_id": player.telegram_id, "display_name": player.display_name,
            "phone_number": player.phone_number, "is_banned": player.is_banned, "ban_reason": player.ban_reason,
            "is_frozen": player.is_frozen, "frozen_reason": player.frozen_reason,
            "daily_deposit_limit": player.daily_deposit_limit, "daily_withdrawal_limit": player.daily_withdrawal_limit,
            "created_at": player.created_at.isoformat(),
        },
        "platform_limits": {
            "deposit": cfg["money"]["daily_deposit_limit"], "withdrawal": cfg["money"]["daily_withdrawal_limit"],
        },
        "wallet": wallet.model_dump(mode="json") if wallet else None,
        "used_24h": {
            "deposit": await control.player_repo.sum_recent(player_id, "deposit", 24),
            "withdrawal": await control.player_repo.sum_recent(player_id, "withdrawal", 24),
        },
        "notes": [{**n, "created_at": n["created_at"].isoformat()} for n in notes],
        "transactions": [
            {"id": t.id, "type": t.type, "amount": t.amount, "status": t.status, "note": t.note,
             "payment_method": t.payment_method, "created_at": t.created_at.isoformat()}
            for t in txns
        ],
        "audit": [{**a, "created_at": a["created_at"].isoformat()} for a in trail],
    }


@router.post("/api/admin/users/{player_id}/freeze")
async def player_freeze(player_id: str, request: Request, payload: dict = Body(default={}),
                        admin_id: str = Depends(require_perm("players.ban"))):
    player = await _player_or_404(player_id)
    reason = ((payload or {}).get("reason") or "Account under review").strip()[:300]
    control = get_control_service()
    await control.player_repo.set_frozen(player_id, True, reason)
    await control.audit(admin_id, "player.freeze", target_type="player", target_id=player_id, summary=reason,
                        before={"is_frozen": bool(player.is_frozen)}, after={"is_frozen": True}, ip=client_ip(request))
    return {"success": True, "is_frozen": True}


@router.post("/api/admin/users/{player_id}/unfreeze")
async def player_unfreeze(player_id: str, request: Request, admin_id: str = Depends(require_perm("players.ban"))):
    player = await _player_or_404(player_id)
    control = get_control_service()
    await control.player_repo.set_frozen(player_id, False)
    await control.audit(admin_id, "player.unfreeze", target_type="player", target_id=player_id,
                        before={"is_frozen": bool(player.is_frozen)}, after={"is_frozen": False}, ip=client_ip(request))
    return {"success": True, "is_frozen": False}


def _limit(value, name: str) -> Optional[int]:
    if value in (None, ""):
        return None  # back to the platform default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name} must be a whole number")
    if n < 0 or n > 100_000_000:
        raise HTTPException(status_code=400, detail=f"{name} must be between 0 and 100000000")
    return n


@router.put("/api/admin/users/{player_id}/limits")
async def player_set_limits(player_id: str, request: Request, payload: dict = Body(...),
                            admin_id: str = Depends(require_perm("players.notes"))):
    player = await _player_or_404(player_id)
    dep = _limit(payload.get("daily_deposit_limit"), "daily_deposit_limit")
    wd = _limit(payload.get("daily_withdrawal_limit"), "daily_withdrawal_limit")
    control = get_control_service()
    await control.player_repo.set_limits(player_id, dep, wd)
    await control.audit(
        admin_id, "player.limits", target_type="player", target_id=player_id,
        summary="Personal daily limits updated",
        before={"deposit": player.daily_deposit_limit, "withdrawal": player.daily_withdrawal_limit},
        after={"deposit": dep, "withdrawal": wd}, ip=client_ip(request),
    )
    return {"success": True, "daily_deposit_limit": dep, "daily_withdrawal_limit": wd}


@router.post("/api/admin/users/{player_id}/notes")
async def player_add_note(player_id: str, request: Request, payload: dict = Body(...),
                          admin_id: str = Depends(require_perm("players.notes"))):
    await _player_or_404(player_id)
    note = (payload.get("note") or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="Write a note first")
    if len(note) > 1000:
        raise HTTPException(status_code=400, detail="Notes are limited to 1000 characters")
    control = get_control_service()
    saved = await control.player_repo.add_note(player_id, admin_id, note)
    await control.audit(admin_id, "player.note", target_type="player", target_id=player_id,
                        summary=note[:120], ip=client_ip(request))
    return saved


@router.delete("/api/admin/users/{player_id}/notes/{note_id}")
async def player_delete_note(player_id: str, note_id: str, request: Request,
                             admin_id: str = Depends(require_perm("players.notes"))):
    control = get_control_service()
    await control.player_repo.delete_note(note_id)
    await control.audit(admin_id, "player.note_delete", target_type="player", target_id=player_id,
                        summary=f"Deleted note {note_id}", ip=client_ip(request))
    return {"success": True}


@router.post("/api/admin/users/{player_id}/wallet-adjust")
async def player_wallet_adjust(player_id: str, request: Request, payload: dict = Body(...),
                               admin_id: str = Depends(require_perm("players.wallet_adjust"))):
    """Add or remove money using atomic deltas. kind: real (playable) | winnings (withdrawable) | bonus."""
    await _player_or_404(player_id)
    kind = payload.get("kind")
    if kind not in ("real", "winnings", "bonus"):
        raise HTTPException(status_code=400, detail="kind must be real, winnings or bonus")
    try:
        delta = int(payload.get("delta"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="delta must be a whole number")
    if delta == 0 or abs(delta) > 1_000_000:
        raise HTTPException(status_code=400, detail="delta must be non-zero and at most 1,000,000")
    reason = (payload.get("reason") or "").strip()
    if len(reason) < 3:
        raise HTTPException(status_code=400, detail="A reason is required for every wallet adjustment")

    repo = WalletRepository()
    before = await repo.get_by_player(player_id)
    b = before.balance if before else 0
    w = before.won_balance if before else 0
    bo = before.bonus_balance if before else 0
    nb, nw, nbo = b, w, bo
    if kind == "real":
        nb = b + delta
    elif kind == "winnings":
        nb, nw = b + delta, w + delta
    else:
        nbo = bo + delta
    if nb < 0 or nw < 0 or nbo < 0 or nw > nb:
        raise HTTPException(status_code=400, detail="That would leave the wallet negative or winnings above the balance")

    try:
        wallet = await repo.apply_delta(
            player_id,
            balance=delta if kind in ("real", "winnings") else 0,
            won=delta if kind == "winnings" else 0,
            bonus=delta if kind == "bonus" else 0,
        )
    except Exception as e:  # noqa: BLE001 - a DB CHECK constraint lost a race
        logger.warning("Wallet adjust rejected for %s: %s", player_id, e)
        raise HTTPException(status_code=409, detail="Wallet changed at the same moment - please retry")
    await TransactionRepository().create(Transaction(
        player_id=player_id, type="admin_adjustment", amount=delta, balance_after=wallet.balance,
        status="completed", admin_id=admin_id, note=f"[{kind}] {reason}",
    ))
    await get_control_service().audit(
        admin_id, "wallet.adjust", target_type="player", target_id=player_id, summary=f"{delta:+d} ETB {kind}: {reason}",
        before={"balance": b, "won_balance": w, "bonus_balance": bo},
        after={"balance": wallet.balance, "won_balance": wallet.won_balance, "bonus_balance": wallet.bonus_balance},
        ip=client_ip(request),
    )
    threshold = int((await get_control_service().get_published())["alerts"].get("big_wallet_adjust_amount", 0) or 0)
    if threshold and abs(delta) >= threshold:
        player = await PlayerRepository().get_by_id(player_id)
        get_alert_service().schedule("wallet_adjust", {
            "delta": delta, "kind": kind, "reason": reason, "actor": admin_id,
            "player_name": player.display_name if player else player_id,
        })
    return {"success": True, "balance": wallet.balance, "won_balance": wallet.won_balance, "bonus_balance": wallet.bonus_balance}


# ---------------------------------------------------------------------------
# Live dashboard
# ---------------------------------------------------------------------------

@router.get("/api/admin/live")
async def live_dashboard(admin_id: str = Depends(require_admin_dep)):
    """Everything the operations dashboard shows, in one round trip."""
    from datetime import datetime, timezone

    from src.services.room_service import RoomService

    repo = LiveStatsRepository()
    control = get_control_service()
    now = datetime.now(timezone.utc)
    win = live_logic.day_windows(now, env_settings.dashboard_utc_offset_hours)

    today = await repo.window_stats(win["today_start"], now)
    yesterday = await repo.window_stats(win["yesterday_start"], win["today_start"])
    for d in (today, yesterday):
        d["revenue"] = live_logic.ggr(d.get("stakes", 0), d.get("payouts", 0), d.get("refunds", 0))
    today["new_players"] = await repo.new_players(win["today_start"])
    yesterday["new_players"] = await repo.new_players(win["yesterday_start"]) - today["new_players"]
    rounds = await repo.rounds_since(win["today_start"])

    pend_raw = await repo.pending()
    pending = {}
    for key, short in (("top_up_pending", "deposit"), ("withdrawal_pending", "withdrawal")):
        p = pend_raw.get(key, {"count": 0, "amount": 0, "oldest": None})
        pending[short] = {"count": p["count"], "amount": p["amount"], "oldest_minutes": live_logic.age_minutes(p["oldest"], now)}

    playing, rooms = 0, []
    for track in RoomService.TRACKS:
        room = await get_room_service().rooms.get_active(track)
        if room:
            playing += len(room.taken_cartelas)
            rooms.append({"track": track, "state": room.state, "players": len(room.taken_cartelas),
                          "prize_pool": room.prize_pool or 0, "called": len(room.called_numbers)})

    since = win["today_start"] - timedelta(days=7)
    stake_rows, deposit_rows = await repo.daily_rows(since, env_settings.dashboard_utc_offset_hours)
    series = live_logic.build_series(stake_rows, deposit_rows, win["local_date"], 7)

    cfg = await control.get_published()
    snap_row = await control.repo.get()
    live = {
        "generated_at": now.isoformat(),
        "connections": sum(len(v) for v in manager.active_connections.values()),
        "playing_now": playing,
        "rooms": rooms,
        "today": {**today, "rounds_started": rounds["started"], "rounds_ended": rounds["ended"]},
        "yesterday": yesterday,
        "change": {
            "revenue": live_logic.change_pct(today["revenue"], yesterday["revenue"]),
            "deposits": live_logic.change_pct(today["deposits"], yesterday["deposits"]),
            "players_played": live_logic.change_pct(today["players_played"], yesterday["players_played"]),
        },
        "pending": pending,
        "series": series,
        "unpublished_changes": len(_unpublished(cfg, snap_row)),
    }
    live["alerts"] = live_logic.build_alerts(live, cfg)
    return live


def _unpublished(published_cfg: dict, row: dict) -> list:
    draft = row.get("draft")
    if not draft:
        return []
    try:
        cfg = cl.normalize_config(draft)
        for key in ("maintenance", "kill_switch"):   # emergency controls are never part of a draft
            cfg[key] = published_cfg[key]
        return cl.diff_config(published_cfg, cfg)
    except ValueError:
        return []


# ---------------------------------------------------------------------------
# Telegram alerts
# ---------------------------------------------------------------------------

@router.get("/api/admin/alerts")
async def alerts_get(admin_id: str = Depends(require_admin_dep)):
    admin = await get_admin_service().get_admin(admin_id)
    role = "super" if (admin and admin.is_super_admin) else (admin.role if admin else "admin")
    custom = admin.alert_types if admin else None
    effective = cl.effective_alert_types(role, custom, bool(admin and admin.is_super_admin))
    cfg = await get_control_service().get_published()
    return {
        "types": [{"key": k, "label": v[0], "description": v[1], "group": k in cl.ALERT_GROUP_TYPES} for k, v in cl.ALERT_TYPES.items()],
        "mine": sorted(effective),
        "custom": custom is not None,
        "can_customise": admin is not None,
        "thresholds": cfg["alerts"],
        "delivery": {"bot_configured": bool(env_settings.telegram_bot_token), "group_chat": bool(env_settings.admin_chat_id)},
    }


@router.put("/api/admin/alerts/mine")
async def alerts_set_mine(request: Request, payload: dict = Body(...), admin_id: str = Depends(require_admin_dep)):
    admin = await get_admin_service().get_admin(admin_id)
    if admin is None:
        raise HTTPException(status_code=400, detail="Ask a super admin to add you to the staff list to customise alerts")
    try:
        types = cl.validate_alert_types(payload.get("types"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await AdminRepository().set_alert_types(admin_id, types)
    await get_control_service().audit(admin_id, "alerts.subscribe", target_type="admin", target_id=admin_id,
                                      summary="Reset to role defaults" if types is None else f"{len(types)} alert type(s)",
                                      after={"types": types}, ip=client_ip(request))
    return {"success": True, "custom": types is not None}


@router.post("/api/admin/alerts/test")
async def alerts_test(admin_id: str = Depends(require_admin_dep)):
    """Send a test message to the caller only, so they can confirm Telegram delivery works."""
    result = await get_alert_service().send("test", cl.format_alert("test", {}), only_to=[admin_id])
    if result["sent"]:
        return {"success": True}
    reason = "; ".join(result["errors"]) or "unknown"
    hint = "Open the bot in Telegram and press Start, then try again." if "Forbidden" in reason or "BadRequest" in reason else ""
    raise HTTPException(status_code=502, detail=f"Could not deliver ({reason}). {hint}".strip())
