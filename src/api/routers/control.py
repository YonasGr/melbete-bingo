"""Admin Control Centre API: draft/publish config, emergency switches, audit log, player controls.

Admin endpoints live under /api/admin (cookie session, role-based permissions); the single public
endpoint, GET /api/control/public, is what the player app polls for maintenance, flags and content.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

import asyncio
from datetime import datetime, timedelta, timezone

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
        snapshot = await get_control_service().publish(admin_id, ip=client_ip(request))
    except ControlError as e:
        _raise(e)
    from src.services.lobby_sync import sync_waiting_lobbies

    if isinstance(snapshot, dict):
        snapshot["lobby_sync"] = await sync_waiting_lobbies()
    return snapshot


@router.post("/api/admin/control/rollback")
async def control_rollback(request: Request, admin_id: str = Depends(require_admin_dep)):
    try:
        snapshot = await get_control_service().rollback(admin_id, ip=client_ip(request))
    except ControlError as e:
        _raise(e)
    from src.services.lobby_sync import sync_waiting_lobbies

    if isinstance(snapshot, dict):
        snapshot["lobby_sync"] = await sync_waiting_lobbies()
    return snapshot


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


# ---------------------------------------------------------------------------
# Rewards control board: start / stop each promotional reward, timers, pause-all, history + undo
# ---------------------------------------------------------------------------

_REWARD_TX_TYPES = {
    "missions": ("mission_reward",),
    "streak_winback": ("streak_reward", "winback_bonus"),
    "leaderboard_prizes": ("leaderboard_reward",),
}
_CAMPAIGN_REWARDS = ("welcome", "daily", "first_deposit", "referral", "cashback")


async def _reward_stats(days: int = 7) -> tuple[dict, dict, list]:
    """(campaigns, series, dates): campaign rows by reward, {reward: {date: (n, etb)}}, last `days` Ethiopia dates."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.core.db import get_pool
    from src.services.bonus_service import BonusService

    addis = ZoneInfo("Africa/Addis_Ababa")
    today = datetime.now(addis).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    campaigns: dict = {}
    series: dict = {}
    try:
        pool = await get_pool()
        for r in await pool.fetch("SELECT reward_type, enabled, amount_etb, daily_budget_etb, total_budget_etb FROM bonus_campaigns"):
            campaigns[r["reward_type"]] = dict(r)
        for r in await pool.fetch(
            "SELECT (created_at AT TIME ZONE 'Africa/Addis_Ababa')::date AS d, reward_type AS k, COUNT(*) AS n, COALESCE(SUM(amount_etb),0) AS etb "
            "FROM bonus_ledger WHERE entry_type='grant' AND amount_etb > 0 AND created_at >= now() - interval '9 days' GROUP BY d, k"
        ):
            series.setdefault(r["k"], {})[r["d"].isoformat()] = (int(r["n"]), int(r["etb"]))
        for key, types in _REWARD_TX_TYPES.items():
            for r in await pool.fetch(
                "SELECT (created_at AT TIME ZONE 'Africa/Addis_Ababa')::date AS d, COUNT(*) AS n, COALESCE(SUM(amount),0) AS etb "
                "FROM transactions WHERE type = ANY($1) AND created_at >= now() - interval '9 days' GROUP BY d", list(types)
            ):
                cur = series.setdefault(key, {}).get(r["d"].isoformat(), (0, 0))
                series[key][r["d"].isoformat()] = (cur[0] + int(r["n"]), cur[1] + int(r["etb"]))
    except Exception:  # noqa: BLE001 - the board still works (switches) without the numbers
        logger.exception("reward stats unavailable")
    usage = {}
    try:
        usage = await BonusService().budget_usage()
    except Exception:  # noqa: BLE001
        logger.exception("reward budget usage unavailable")
    return campaigns, {"series": series, "usage": usage}, dates


@router.get("/api/admin/rewards/board")
async def rewards_board(admin_id: str = Depends(require_admin_dep)):
    svc = get_control_service()
    row = await svc.repo.get()
    cfg = cl.normalize_config(row.get("published") or {})
    campaigns, extra, dates = await _reward_stats()
    items = []
    for key, label in cl.REWARDS.items():
        st = cl.reward_state(cfg, key)
        ser = extra["series"].get(key, {})
        days = [{"date": d, "claims": ser.get(d, (0, 0))[0], "etb": ser.get(d, (0, 0))[1]} for d in dates]
        use = extra["usage"].get(key) or ({"today_etb": days[-1]["etb"], "total_etb": None, "today_count": days[-1]["claims"]}
                                            if key in _REWARD_TX_TYPES else None)
        items.append({"key": key, "label": label, "config": cfg["rewards"]["items"][key], "state": st["state"], "message": st["message"],
                      "preview": _reward_preview(cfg["rewards"]["items"][key], st, label),
                      "campaign": campaigns.get(key) if key in _CAMPAIGN_REWARDS else None, "usage": use, "days": days})
    history = []
    for r in await svc.audit_repo.list(limit=25, action_prefix="rewards."):
        history.append({"id": r["id"], "created_at": r["created_at"].isoformat(), "actor_id": r["actor_id"], "actor_role": r["actor_role"],
                        "action": r["action"], "summary": r["summary"], "undoable": bool(r.get("before_json"))})
    perms = await svc.permissions(admin_id)
    return {"master": cfg["rewards"]["master"], "items": items, "history": history, "can_edit": "rewards.manage" in perms}


@router.post("/api/admin/rewards/action")
async def rewards_action(request: Request, payload: dict = Body(...), admin_id: str = Depends(require_admin_dep)):
    """Instant, audited: stop / start one reward, set its timer or budget auto-stop, pause or resume all."""
    import copy

    svc = get_control_service()
    op, key = payload.get("op"), payload.get("key")
    cur = cl.normalize_config((await svc.repo.get()).get("published") or {})["rewards"]
    new = copy.deepcopy(cur)
    reason = str(payload.get("reason") or "").strip()[:200]
    mode = payload.get("mode") or "new"
    if mode not in cl.REWARD_MODES:
        raise HTTPException(status_code=400, detail="Unknown stop mode")
    label = cl.REWARDS.get(key, "")
    if op in ("stop", "start", "schedule", "auto_stop", "flash", "windows", "elig", "spike") and key not in cl.REWARDS:
        raise HTTPException(status_code=400, detail="Unknown reward")
    how = "new claims only" if mode == "new" else "everything held, including earned rewards"
    if op == "stop":
        new["items"][key].update(enabled=False, mode=mode, reason=reason)
        summary = f"{label} stopped ({how})" + (f": {reason}" if reason else "")
    elif op == "start":
        it = new["items"][key]
        now = datetime.now(timezone.utc)
        stop, start = cl._parse_dt(it["stop_at"]), cl._parse_dt(it["start_at"])
        it.update(enabled=True, reason="", stop_at="" if (stop and stop <= now) else it["stop_at"],
                  start_at="" if (start and start > now) else it["start_at"])
        summary = f"{label} started"
        if key in _CAMPAIGN_REWARDS:
            from src.core.db import get_pool
            await (await get_pool()).execute("UPDATE bonus_campaigns SET enabled = TRUE, updated_at = now() WHERE reward_type = $1", key)
            summary += " (campaign switched on)"
    elif op == "schedule":
        new["items"][key].update(start_at=str(payload.get("start_at") or ""), stop_at=str(payload.get("stop_at") or ""))
        summary = f"{label} timer: start {new['items'][key]['start_at'] or '-'}, stop {new['items'][key]['stop_at'] or '-'}"
    elif op == "auto_stop":
        new["items"][key]["auto_stop_pct"] = int(payload.get("auto_stop_pct") or 0)
        summary = f"{label} auto-stop at {new['items'][key]['auto_stop_pct']}% of budget" if new["items"][key]["auto_stop_pct"] else f"{label} budget auto-stop off"
    elif op == "flash":
        mins = int(payload.get("minutes") or 0)
        if mins < 5 or mins > 7 * 24 * 60:
            raise HTTPException(status_code=400, detail="Flash promos run from 5 minutes to 7 days")
        end = datetime.now(timezone.utc) + timedelta(minutes=mins)
        new["items"][key].update(enabled=True, reason="", start_at="", stop_at=end.isoformat(timespec="seconds"))
        summary = f"{label} flash promo: runs for {mins // 60}h {mins % 60}m, then stops by itself"
        if key in _CAMPAIGN_REWARDS:
            from src.core.db import get_pool
            await (await get_pool()).execute("UPDATE bonus_campaigns SET enabled = TRUE, updated_at = now() WHERE reward_type = $1", key)
    elif op == "windows":
        new["items"][key]["windows"] = payload.get("windows") or []
        summary = f"{label} weekly windows updated ({len(new['items'][key]['windows'])} window(s); none = always)"
    elif op == "elig":
        new["items"][key]["elig"] = {"new_players_days": int(payload.get("new_players_days") or 0), "min_deposits_etb": int(payload.get("min_deposits_etb") or 0)}
        summary = f"{label} eligibility: new players within {new['items'][key]['elig']['new_players_days'] or 'any'} days, min deposits {new['items'][key]['elig']['min_deposits_etb']} ETB"
    elif op == "spike":
        new["items"][key].update(spike_limit=int(payload.get("spike_limit") or 0), spike_minutes=int(payload.get("spike_minutes") or 10))
        it = new["items"][key]
        summary = f"{label} abuse brake: " + (f"stop after {it['spike_limit']} claims in {it['spike_minutes']} min" if it["spike_limit"] else "off")
    elif op == "pause_all":
        new["master"] = {"paused": True, "mode": mode, "reason": reason}
        summary = f"ALL rewards paused ({how})" + (f": {reason}" if reason else "")
    elif op == "resume_all":
        new["master"] = {"paused": False, "mode": "new", "reason": ""}
        summary = "All rewards resumed (each reward keeps its own switch)"
    else:
        raise HTTPException(status_code=400, detail="Unknown action")
    notify = bool(payload.get("notify")) and op in ("start", "flash", "stop")
    try:
        await svc.apply_rewards(admin_id, new, action=op, summary=summary + (" (players notified)" if notify else ""), key=key,
                                ip=client_ip(request), alert=op in ("stop", "pause_all"))
    except ControlError as e:
        _raise(e)
    if notify:
        text = (f"⏸ {label} is paused for now." if op == "stop" else
                f"🎁 {label} is live for a limited time - claim it in the Bonus Center!" if op == "flash" else f"🎁 {label} is live - open the Bonus Center to claim it!")
        asyncio.create_task(_broadcast_safely(text))
    return await rewards_board(admin_id=admin_id)


@router.post("/api/admin/rewards/undo")
async def rewards_undo(request: Request, payload: dict = Body(...), admin_id: str = Depends(require_admin_dep)):
    """Put the rewards settings back to how they were before one of the last changes."""
    import json

    svc = get_control_service()
    try:
        wanted = int(payload.get("audit_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="audit_id is required")
    hit = next((r for r in await svc.audit_repo.list(limit=100, action_prefix="rewards.") if r["id"] == wanted), None)
    if not hit or not hit.get("before_json"):
        raise HTTPException(status_code=404, detail="That change can't be undone")
    before = hit["before_json"]
    if isinstance(before, str):
        before = json.loads(before)
    try:
        await svc.apply_rewards(admin_id, before, action="undo", key=hit.get("target_id"), ip=client_ip(request),
                                summary=f"Undid change #{wanted}: {hit.get('summary') or hit['action']}")
    except ControlError as e:
        _raise(e)
    return await rewards_board(admin_id=admin_id)


def _reward_preview(item: dict, st: dict, label: str) -> str:
    """One line: what players see in the Bonus Center for this reward right now."""
    if st["state"] == "on":
        return f"{label}: claimable now" + (f" · ends {item['stop_at'][:16].replace('T', ' ')}" if item.get("stop_at") else "")
    if st["state"] == "scheduled":
        return f"{label}: shows \"{st['message']}\" (starts {item['start_at'][:16].replace('T', ' ')})"
    return f"{label}: shows \"{st['message']}\""


async def _broadcast_safely(text: str) -> None:
    try:
        from src.services.broadcast_service import BroadcastService
        await BroadcastService().send(text)
    except Exception:  # noqa: BLE001 - a failed notice must never undo the admin's change
        logger.exception("reward notice broadcast failed")


@router.get("/api/admin/rewards/overrides")
async def rewards_overrides(admin_id: str = Depends(require_admin_dep)):
    from src.core.db import get_pool
    rows = await (await get_pool()).fetch(
        "SELECT o.player_id, o.note, o.created_by, o.created_at, p.display_name FROM reward_player_overrides o "
        "LEFT JOIN players p ON p.id = o.player_id ORDER BY o.created_at DESC LIMIT 200")
    return {"items": [{"player_id": r["player_id"], "name": r["display_name"], "note": r["note"], "by": r["created_by"], "at": r["created_at"].isoformat()} for r in rows]}


@router.post("/api/admin/rewards/overrides")
async def rewards_override_add(request: Request, payload: dict = Body(...), admin_id: str = Depends(require_admin_dep)):
    """Block one player from every reward (claims, streaks, missions...). Remove with DELETE."""
    from src.core.db import get_pool
    svc = get_control_service()
    await svc.require(admin_id, "rewards.manage")
    pid = str(payload.get("player_id") or "").strip()
    pool = await get_pool()
    if not pid or not await pool.fetchval("SELECT 1 FROM players WHERE id = $1", pid):
        raise HTTPException(status_code=404, detail="Player not found (use the id like tg_123456789)")
    note = str(payload.get("note") or "").strip()[:200]
    await pool.execute("INSERT INTO reward_player_overrides (player_id, note, created_by) VALUES ($1,$2,$3) "
                       "ON CONFLICT (player_id) DO UPDATE SET note = EXCLUDED.note, created_by = EXCLUDED.created_by", pid, note, str(admin_id))
    await svc.audit(admin_id, "rewards.override_add", target_type="player", target_id=pid, ip=client_ip(request), summary=f"Blocked {pid} from all rewards" + (f": {note}" if note else ""))
    return await rewards_overrides(admin_id=admin_id)


@router.delete("/api/admin/rewards/overrides/{player_id}")
async def rewards_override_remove(player_id: str, request: Request, admin_id: str = Depends(require_admin_dep)):
    from src.core.db import get_pool
    svc = get_control_service()
    await svc.require(admin_id, "rewards.manage")
    await (await get_pool()).execute("DELETE FROM reward_player_overrides WHERE player_id = $1", player_id)
    await svc.audit(admin_id, "rewards.override_remove", target_type="player", target_id=player_id, ip=client_ip(request), summary=f"Unblocked {player_id} from rewards")
    return await rewards_overrides(admin_id=admin_id)


@router.post("/api/admin/rewards/grant")
async def rewards_manual_grant(request: Request, payload: dict = Body(...), admin_id: str = Depends(require_admin_dep)):
    """Give one player a one-off bonus credit (bonus balance: playable, not withdrawable)."""
    from src.api.dependencies import get_wallet_service
    from src.core.db import get_pool
    svc = get_control_service()
    await svc.require(admin_id, "rewards.manage")
    pid = str(payload.get("player_id") or "").strip()
    try:
        amount = int(payload.get("amount_etb"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Enter the amount in ETB")
    note = str(payload.get("note") or "").strip()[:200]
    if not 1 <= amount <= 10_000:
        raise HTTPException(status_code=400, detail="Amount must be between 1 and 10,000 ETB")
    if not note:
        raise HTTPException(status_code=400, detail="Add a short reason - it is saved in the audit log")
    if not pid or not await (await get_pool()).fetchval("SELECT 1 FROM players WHERE id = $1", pid):
        raise HTTPException(status_code=404, detail="Player not found (use the id like tg_123456789)")
    await get_wallet_service().credit_engagement_bonus(pid, amount, "admin_reward", f"Admin bonus: {note}")
    await svc.audit(admin_id, "rewards.manual_grant", target_type="player", target_id=pid, ip=client_ip(request), summary=f"Granted {amount} ETB bonus to {pid}: {note}")
    return {"ok": True, "player_id": pid, "amount_etb": amount}
