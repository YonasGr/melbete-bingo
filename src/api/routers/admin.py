"""
Admin Panel endpoints (Auth, Broadcasts, Settings, User & Game management, Economy, and Game Admin controls).
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from src.api.idempotency import run_idempotent
from src.services.agent_service import AgentError, AgentNotFoundError, AgentAlreadyExistsError
from src.core.rate_limit import client_ip, enforce as enforce_rate_limit
from src.core.admin_auth import ADMIN_SESSION_TTL_SECONDS, create_admin_session, verify_totp
from src.repositories import PlayerRepository
from src.services import InvalidStateError, RoomNotFoundError, RoomService
from src.services.admin_service import NotSuperAdminError
from src.services.wallet_service import TransactionNotFoundError
from src.services.broadcast_service import BroadcastPhoto, BroadcastService, BroadcastValidationError, validate_buttons
from src.api.dependencies import (
    _decode_image_data_url,
    _resolve_telegram_id,
    get_admin_service,
    get_agent_service,
    get_control_service,
    get_bot_service,
    get_room_service,
    get_wallet_service,
    manager,
    require_admin_dep,
    require_perm,
    require_super_admin_dep,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


async def _audit(request: Request, actor: str, action: str, **kw) -> None:
    """Record an admin action in the audit log (never raises); staff changes also alert admins."""
    await get_control_service().audit(actor, action, ip=client_ip(request), **kw)
    if action.startswith("staff."):
        from src.services.admin_alerts import get_alert_service

        who = f"{kw.get('target_id', '?')}"
        get_alert_service().schedule("staff_change", {"summary": f"{kw.get('summary') or action} ({who})", "actor": actor})

@router.post("/login")
async def admin_login(
    request: Request,
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    # Brute-force guard for the 6-digit TOTP code (checked before any verification work).
    enforce_rate_limit("admin_login", client_ip(request))
    telegram_id = await _resolve_telegram_id(x_telegram_init_data)
    admin_service = get_admin_service()
    known = bool(telegram_id) and await admin_service.is_admin(telegram_id)
    if not telegram_id or not known or not verify_totp(telegram_id, str(payload.get("totp_code", ""))):
        from src.services.admin_alerts import get_alert_service

        await get_alert_service().on_failed_login(client_ip(request), telegram_id, known)
        await _audit(request, telegram_id or "unknown", "admin.login_failed", target_type="admin", target_id=telegram_id or "unknown",
                     summary="Known admin, wrong code" if known else "Not an admin or no Telegram identity")
        raise HTTPException(status_code=403, detail="Telegram verification and a valid authenticator code are required")
    response = JSONResponse({
        "is_admin": True,
        "is_super_admin": await admin_service.is_super_admin(telegram_id),
        "permissions": sorted(await admin_service.permissions_of(telegram_id)),
    })
    response.set_cookie(
        "admin_session", create_admin_session(telegram_id), max_age=ADMIN_SESSION_TTL_SECONDS,
        httponly=True, secure=True, samesite="strict", path="/api/admin",
    )
    return response


@router.post("/logout")
async def admin_logout():
    response = JSONResponse({"message": "Logged out"})
    response.delete_cookie("admin_session", path="/api/admin")
    return response


@router.get("/me")
async def admin_me(admin_id: str = Depends(require_admin_dep)):
    admin_service = get_admin_service()
    admin = await admin_service.get_admin(admin_id)
    perms = await admin_service.permissions_of(admin_id)
    is_super = await admin_service.is_super_admin(admin_id)
    return {
        "is_admin": True,
        "is_super_admin": is_super,
        "role": "super" if is_super else (admin.role if admin else "admin"),
        "permissions": sorted(perms),
    }


@router.get("/uploads/{filename}")
async def admin_deposit_screenshot(filename: str, admin_id: str = Depends(require_admin_dep)):
    if Path(filename).name != filename or not filename.endswith((".jpg", ".png", ".webp")):
        raise HTTPException(status_code=404, detail="Screenshot not found")
    from src.services.screenshot_storage import get_screenshot
    content, mime = get_screenshot(filename)
    if content is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return Response(content=content, media_type=mime)


@router.post("/broadcasts")
async def admin_broadcast(request: Request, background_tasks: BackgroundTasks, payload: dict = Body(...), admin_id: str = Depends(require_admin_dep)):
    from src.repositories.base import BaseRepository
    row = await BaseRepository()._fetchrow("SELECT status FROM broadcast_progress WHERE id = 'active'")
    if row and row["status"] in ("running", "cancelling"):
        raise HTTPException(status_code=400, detail="Another broadcast is already in progress.")

    text = payload.get("text", "")
    photo_base64 = payload.get("photo_base64")
    buttons = payload.get("buttons")

    photo = None
    if photo_base64:
        try:
            _, raw, ext = _decode_image_data_url(
                str(photo_base64), max_bytes=5_000_000, field_name="Broadcast image"
            )
            photo = BroadcastPhoto(content=raw, filename=f"broadcast.{ext}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    try:
        if not text and not photo:
            raise BroadcastValidationError("Write a message or attach an image")
        if len(text) > (1024 if photo else 4096):
            raise BroadcastValidationError("Message is too long for this broadcast format")
        validate_buttons(buttons)
    except BroadcastValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    async def run_broadcast_task():
        try:
            await BroadcastService().send(
                text,
                photo=photo,
                buttons=buttons,
            )
        except Exception:
            logger.exception("Background broadcast failed")
            await BaseRepository()._execute("""
                UPDATE broadcast_progress
                SET status = 'failed', updated_at = now()
                WHERE id = 'active'
            """)

    background_tasks.add_task(run_broadcast_task)
    await _audit(request, admin_id, "broadcast.start", target_type="broadcast", target_id="active", summary="Started a broadcast")
    return {"status": "started", "message": "Broadcast started in background."}


@router.post("/broadcasts/cancel")
async def admin_cancel_broadcast(request: Request, admin_id: str = Depends(require_admin_dep)):
    from src.repositories.base import BaseRepository
    repo = BaseRepository()
    row = await repo._fetchrow("SELECT status FROM broadcast_progress WHERE id = 'active'")
    if not row:
        raise HTTPException(status_code=400, detail="No broadcast has been started.")

    await repo._execute("""
        UPDATE broadcast_progress
        SET status = 'cancelled', updated_at = now()
        WHERE id = 'active'
    """)
    await BroadcastService().cancel()
    await _audit(request, admin_id, "broadcast.cancel", target_type="broadcast", target_id="active", summary="Cancelled the broadcast")
    return {"status": "cancelled", "message": "Broadcast cancelled and state reset."}


@router.get("/broadcasts/status")
async def admin_broadcast_status(admin_id: str = Depends(require_admin_dep)):
    from datetime import datetime, timezone, timedelta
    from src.repositories.base import BaseRepository
    repo = BaseRepository()
    row = await repo._fetchrow("SELECT * FROM broadcast_progress WHERE id = 'active'")
    if not row:
        return {"status": "idle", "total_users": 0, "sent_users": 0, "delivered_users": 0, "failed_users": 0}

    # Automatic recovery of stuck/orphaned states
    if row["status"] in ("cancelling", "running"):
        updated_at = row["updated_at"]
        stale = False
        if row["status"] == "cancelling":
            stale = True
        elif updated_at:
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if (now - updated_at) > timedelta(seconds=45):
                stale = True

        if stale:
            await repo._execute("""
                UPDATE broadcast_progress
                SET status = 'cancelled', updated_at = now()
                WHERE id = 'active'
            """)
            row = await repo._fetchrow("SELECT * FROM broadcast_progress WHERE id = 'active'")

    return {
        "status": row["status"],
        "total_users": row["total_users"],
        "sent_users": row["sent_users"],
        "delivered_users": row["delivered_users"],
        "failed_users": row["failed_users"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None
    }


@router.get("/admins")
async def admin_list_admins(admin_id: str = Depends(require_admin_dep)):
    admins = await get_admin_service().list_admins()
    return {"admins": [{
        "telegram_id": a.telegram_id, "is_super_admin": a.is_super_admin, "added_by": a.added_by,
        "role": "super" if a.is_super_admin else a.role, "extra_permissions": a.extra_permissions,
    } for a in admins]}


@router.post("/admins")
async def admin_add_admin(request: Request, payload: dict = Body(...), requester: str = Depends(require_super_admin_dep)):
    new_id = payload.get("telegram_id")
    if not new_id:
        raise HTTPException(status_code=400, detail="telegram_id is required")
    try:
        admin = await get_admin_service().add_admin(
            requester, str(new_id), role=payload.get("role") or "admin", extra_permissions=payload.get("extra_permissions"),
        )
    except NotSuperAdminError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, requester, "staff.add", target_type="admin", target_id=admin.telegram_id,
                 summary=f"Added staff as {admin.role}", after={"role": admin.role, "extra_permissions": admin.extra_permissions})
    return {"telegram_id": admin.telegram_id, "role": admin.role}


@router.put("/admins/{telegram_id}/role")
async def admin_set_role(telegram_id: str, request: Request, payload: dict = Body(...), requester: str = Depends(require_super_admin_dep)):
    service = get_admin_service()
    before = await service.get_admin(telegram_id)
    try:
        admin = await service.set_role(requester, telegram_id, payload.get("role") or "", payload.get("extra_permissions"))
    except NotSuperAdminError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, requester, "staff.role", target_type="admin", target_id=telegram_id,
                 summary=f"Role set to {admin.role}",
                 before={"role": before.role, "extra_permissions": before.extra_permissions} if before else None,
                 after={"role": admin.role, "extra_permissions": admin.extra_permissions})
    return {"telegram_id": admin.telegram_id, "role": admin.role, "extra_permissions": admin.extra_permissions}


@router.delete("/admins/{telegram_id}")
async def admin_remove_admin(telegram_id: str, request: Request, requester: str = Depends(require_super_admin_dep)):
    try:
        await get_admin_service().remove_admin(requester, telegram_id)
    except NotSuperAdminError as e:
        raise HTTPException(status_code=403, detail=str(e))
    await _audit(request, requester, "staff.remove", target_type="admin", target_id=telegram_id, summary="Removed staff access")
    return {"message": "Admin removed"}


@router.post("/admins/promote")
async def admin_promote_admin(request: Request, payload: dict = Body(...), requester: str = Depends(require_super_admin_dep)):
    try:
        admin = await get_admin_service().promote_to_super_admin(requester, payload["telegram_id"])
    except NotSuperAdminError as e:
        raise HTTPException(status_code=403, detail=str(e))
    await _audit(request, requester, "staff.promote", target_type="admin", target_id=str(payload["telegram_id"]), summary="Promoted to super admin")
    return {"id": admin.id, "telegram_id": admin.telegram_id, "is_super_admin": admin.is_super_admin}


@router.get("/payment-methods")
async def admin_list_payment_methods(admin_id: str = Depends(require_admin_dep)):
    methods = await get_admin_service().list_payment_methods()
    return {
        "methods": [
            {
                "id": m.id,
                "key": m.key,
                "name": m.name,
                "account_info": m.account_info,
                "account_holder": m.account_holder,
                "instructions": m.instructions,
                "active": m.active,
                "supports_deposit": m.supports_deposit,
                "supports_withdrawal": m.supports_withdrawal,
                "display_order": m.display_order,
            }
            for m in methods
        ]
    }


@router.post("/payment-methods")
async def admin_upsert_payment_method(request: Request, payload: dict = Body(...), admin_id: str = Depends(require_admin_dep)):
    method = await get_admin_service().upsert_payment_method(
        key=payload.get("key"),
        name=payload.get("name"),
        account_info=payload.get("account_info"),
        account_holder=payload.get("account_holder"),
        instructions=payload.get("instructions"),
        active=payload.get("active", True),
        supports_deposit=payload.get("supports_deposit", True),
        supports_withdrawal=payload.get("supports_withdrawal", True),
        display_order=payload.get("display_order", 0),
        method_id=payload.get("id"),
    )
    await _audit(request, admin_id, "payment_method.save", target_type="payment_method", target_id=method.id,
                 summary=f"{payload.get('key')} / {payload.get('name')}", after={"active": payload.get("active", True), "key": payload.get("key")})
    return {"id": method.id}


@router.delete("/payment-methods/{method_id}")
async def admin_delete_payment_method(method_id: str, request: Request, admin_id: str = Depends(require_admin_dep)):
    await get_admin_service().delete_payment_method(method_id)
    await _audit(request, admin_id, "payment_method.delete", target_type="payment_method", target_id=method_id, summary="Deleted payment method")
    return {"message": "Payment method deleted"}


@router.get("/settings")
async def admin_get_settings(admin_id: str = Depends(require_admin_dep)):
    s = await get_admin_service().get_settings()
    return s.model_dump()


@router.put("/settings")
async def admin_update_settings(request: Request, payload: dict = Body(...), requester: str = Depends(require_perm("control.publish"))):
    """Direct (live) settings edit. Prefer the Control Centre's draft -> publish flow; this stays for the
    classic Settings form and is limited to roles that may publish."""
    service = get_admin_service()
    before = (await service.get_settings()).model_dump()
    try:
        s = await service.update_settings(requester, payload)
    except NotSuperAdminError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    after = s.model_dump()
    changed = {k: after[k] for k in after if k != "updated_at" and before.get(k) != after[k]}
    await _audit(request, requester, "settings.update", target_type="config", target_id="settings",
                 summary=f"{len(changed)} setting(s) changed", before={k: before.get(k) for k in changed}, after=changed)
    # Settings are read when a lobby is created - push them into the one that is already open too.
    from src.services.lobby_sync import sync_waiting_lobbies

    after["lobby_sync"] = await sync_waiting_lobbies()
    return after


@router.get("/bot-system/status")
async def admin_get_bot_status(admin_id: str = Depends(require_admin_dep)):
    s = await get_admin_service().get_settings()
    bots = await get_bot_service().ensure_bots_initialized()
    return {
        "tracks": {
            "real_10": {"enabled": s.bot_enabled_real_10, "max_bots": s.max_bots_real_10, "stake_amount": s.stake_amount_real_10},
            "real_50": {"enabled": s.bot_enabled_real_50, "max_bots": s.max_bots_real_50, "stake_amount": s.stake_amount_real_50},
            "bonus": {"enabled": s.bot_enabled_bonus, "max_bots": s.max_bots_bonus, "stake_amount": s.stake_amount_bonus},
        },
        "bot_system_enabled": s.bot_enabled_real_10,
        "deposits_enabled": s.deposits_enabled,
        "bot_count": len(bots),
        "bots": [b.model_dump() for b in bots],
    }


@router.post("/bot-system/toggle")
async def admin_toggle_bot_system(request: Request, payload: dict = Body(...), requester: str = Depends(require_admin_dep)):
    enabled = bool(payload.get("enabled", False))
    track = payload.get("track", "real_10")
    if track not in RoomService.TRACKS:
        raise HTTPException(status_code=400, detail=f"Unknown track '{track}'")
    s = await get_admin_service().toggle_bot_system(requester, enabled, track=track)
    await _audit(request, requester, "settings.bots_toggle", target_type="config", target_id=f"bots:{track}",
                 summary=f"Bots {'enabled' if enabled else 'disabled'} on {track}")
    if enabled:
        room = await get_room_service().get_or_create_waiting_session(track)
        if room.state == "waiting":
            await get_bot_service().fill_room_with_bots(get_room_service(), room.id)
    return {"track": track, "enabled": enabled}


@router.post("/deposits/toggle")
async def admin_toggle_deposits(request: Request, payload: dict = Body(...), requester: str = Depends(require_admin_dep)):
    enabled = bool(payload.get("enabled", True))
    s = await get_admin_service().toggle_deposits(requester, enabled)
    await _audit(request, requester, "settings.deposits_toggle", target_type="config", target_id="deposits_enabled",
                 summary=f"Deposits {'enabled' if enabled else 'disabled'}")
    return {"deposits_enabled": s.deposits_enabled}


@router.get("/deposits/pending")
async def admin_pending_deposits(admin_id: str = Depends(require_admin_dep)):
    txns = await get_wallet_service().list_pending_topups()
    player_repo = PlayerRepository()
    out = []
    for t in txns:
        p = await player_repo.get_by_id(t.player_id)
        out.append({
            "id": t.id, "player_id": t.player_id, "player_name": p.display_name if p else t.player_id,
            "amount": t.amount, "payment_method": t.payment_method, "created_at": t.created_at.isoformat(),
            "screenshot_url": t.screenshot_url, "sms_text": t.sms_text,
            "payment_verification_url": t.payment_verification_url, "status": "pending",
        })
    return {"deposits": out}


@router.get("/deposits/history")
async def admin_deposits_history(status: Optional[str] = None, limit: int = 200, admin_id: str = Depends(require_admin_dep)):
    txns = await get_wallet_service().list_deposit_history(limit=limit, status_filter=status)
    player_repo = PlayerRepository()
    out = []
    for t in txns:
        p = await player_repo.get_by_id(t.player_id)
        status_label = "approved" if t.type == "top_up_approved" else ("rejected" if t.type == "top_up_rejected" else "pending")
        out.append({
            "id": t.id, "player_id": t.player_id, "player_name": p.display_name if p else t.player_id,
            "amount": t.amount, "payment_method": t.payment_method, "created_at": t.created_at.isoformat(),
            "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
            "screenshot_url": t.screenshot_url, "sms_text": t.sms_text,
            "payment_verification_url": t.payment_verification_url, "status": status_label,
            "type": t.type, "reviewed_by": t.admin_id, "note": t.note,
        })
    return {"deposits": out}


async def _reconcile_statement(payload: dict) -> dict:
    from src.services.statement_match import StatementError, match_deposits, parse_statement

    text = payload.get("statement")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="Paste the statement (CSV with a header row)")
    if len(text) > 2_000_000:
        raise HTTPException(status_code=400, detail="Statement is too large")
    try:
        parsed = parse_statement(text, payload.get("ref_column") or None, payload.get("amount_column") or None)
    except StatementError as e:
        raise HTTPException(status_code=400, detail=e.message)
    pending = await get_wallet_service().transaction_repo.list_pending_topups()  # oldest first
    deposits = [{"id": t.id, "player_id": t.player_id, "amount": int(t.amount),
                 "text": " ".join(x for x in (t.sms_text, t.payment_verification_url, t.note) if x)} for t in pending]
    result = match_deposits(parsed["rows"], deposits)
    result["columns"] = {"reference": parsed["ref_column"], "amount": parsed["amount_column"]}
    result["statement_rows"] = len(parsed["rows"])
    result["pending_deposits"] = len(deposits)
    return result


@router.post("/deposits/reconcile")
async def admin_reconcile_deposits(payload: dict = Body(...), admin_id: str = Depends(require_admin_dep)):
    """Body: {"statement": "<csv text>", "ref_column": "optional", "amount_column": "optional"}.
    Read-only: compares pending deposits with the statement and returns the buckets."""
    return await _reconcile_statement(payload)


@router.post("/deposits/approve-matched")
async def admin_approve_matched_deposits(request: Request, payload: dict = Body(...), admin_id: str = Depends(require_perm("money.approve"))):
    """Re-runs the match on the server and approves ONLY deposits whose reference AND amount are in the
    statement (never trusts a client-supplied id list on its own). Each approval is the normal atomic,
    idempotent approve_topup, so a retry or a double click cannot credit twice."""
    result = await _reconcile_statement(payload)
    allowed = {m["id"] for m in result["matched"]}
    wanted = payload.get("transaction_ids")
    if wanted is not None:
        if not isinstance(wanted, list) or not all(isinstance(i, str) for i in wanted):
            raise HTTPException(status_code=400, detail="transaction_ids must be a list of ids")
        allowed &= set(wanted)
    ids = sorted(allowed)[:50]  # bounded batch; run again for the rest
    approved, failed = 0, []
    for tid in ids:
        try:
            await get_wallet_service().approve_topup(tid, admin_id)
            approved += 1
        except Exception as e:  # noqa: BLE001 - report and continue with the others
            logger.exception("Bulk approve failed for deposit %s", tid)
            failed.append({"id": tid, "error": str(e) or e.__class__.__name__})
    logger.warning("Admin %s bulk-approved %d statement-matched deposits (%d failed)", admin_id, approved, len(failed))
    await _audit(request, admin_id, "deposit.bulk_approve", target_type="transaction", target_id="bulk",
                 summary=f"{approved} approved, {len(failed)} failed")
    return {"approved": approved, "failed": failed, "remaining_matched": max(0, len(result["matched"]) - approved)}


@router.post("/deposits/{transaction_id}/approve")
async def admin_approve_deposit(transaction_id: str, request: Request, admin_id: str = Depends(require_perm("money.approve"))):
    try:
        txn = await get_wallet_service().approve_topup(transaction_id, admin_id)
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail="Transaction not found")
    await _audit(request, admin_id, "deposit.approve", target_type="transaction", target_id=transaction_id,
                 summary=f"{txn.amount} ETB for {txn.player_id}", after={"status": txn.status, "amount": txn.amount})
    return {"status": txn.status}


@router.post("/deposits/{transaction_id}/reject")
async def admin_reject_deposit(transaction_id: str, request: Request, payload: dict = Body(default={}), admin_id: str = Depends(require_perm("money.approve"))):
    try:
        txn = await get_wallet_service().reject_topup(transaction_id, admin_id, payload.get("note", ""))
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail="Transaction not found")
    await _audit(request, admin_id, "deposit.reject", target_type="transaction", target_id=transaction_id,
                 summary=f"{txn.amount} ETB for {txn.player_id}", after={"status": txn.status, "note": payload.get("note", "")})
    return {"status": txn.status}


async def _big_withdrawal_threshold() -> int:
    """The Control Center 'large withdrawal' amount (0 = off). Same value the Telegram alert uses."""
    try:
        from src.services.control_service import get_control_service

        cfg = await get_control_service().get_published()
        return int(cfg.get("alerts", {}).get("big_withdrawal_amount", 0) or 0)
    except Exception:  # noqa: BLE001 - the flag is a convenience; the list must still load
        return 0


@router.get("/withdrawals/pending")
async def admin_pending_withdrawals(admin_id: str = Depends(require_admin_dep)):
    txns = await get_wallet_service().list_pending_withdrawals()
    player_repo = PlayerRepository()
    threshold = await _big_withdrawal_threshold()
    out = []
    for t in txns:
        p = await player_repo.get_by_id(t.player_id)
        out.append({
            "id": t.id, "player_id": t.player_id, "player_name": p.display_name if p else t.player_id,
            "amount": abs(t.amount), "payment_method": t.payment_method, "legal_name": t.legal_name,
            "withdrawal_address": t.withdrawal_address, "created_at": t.created_at.isoformat(),
            "status": "pending", "is_large": bool(threshold) and abs(t.amount) >= threshold,
        })
    return {"withdrawals": out, "big_threshold": threshold}


@router.get("/withdrawals/history")
async def admin_withdrawals_history(status: Optional[str] = None, limit: int = 200, admin_id: str = Depends(require_admin_dep)):
    txns = await get_wallet_service().list_withdrawal_history(limit=limit, status_filter=status)
    player_repo = PlayerRepository()
    out = []
    for t in txns:
        p = await player_repo.get_by_id(t.player_id)
        status_label = "approved" if t.type == "withdrawal_approved" else ("rejected" if t.type == "withdrawal_rejected" else "pending")
        out.append({
            "id": t.id, "player_id": t.player_id, "player_name": p.display_name if p else t.player_id,
            "amount": abs(t.amount), "payment_method": t.payment_method, "legal_name": t.legal_name,
            "withdrawal_address": t.withdrawal_address, "created_at": t.created_at.isoformat(),
            "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
            "status": status_label, "type": t.type, "reviewed_by": t.admin_id, "note": t.note,
        })
    return {"withdrawals": out}


@router.post("/withdrawals/{transaction_id}/approve")
async def admin_approve_withdrawal(transaction_id: str, request: Request, admin_id: str = Depends(require_perm("money.approve"))):
    wallet = get_wallet_service()
    pending = await wallet.transaction_repo.get(transaction_id)
    if pending is not None and await get_control_service().withdrawal_needs_senior(pending.amount):
        if "money.approve_large" not in await get_admin_service().permissions_of(admin_id):
            raise HTTPException(status_code=403, detail="This withdrawal is above the review threshold and needs a senior approver")
    try:
        txn = await wallet.approve_withdrawal(transaction_id, admin_id)
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail="Transaction not found")
    await _audit(request, admin_id, "withdrawal.approve", target_type="transaction", target_id=transaction_id,
                 summary=f"{abs(txn.amount)} ETB for {txn.player_id}", after={"status": txn.status, "amount": abs(txn.amount)})
    return {"status": txn.status}


@router.post("/withdrawals/{transaction_id}/reject")
async def admin_reject_withdrawal(transaction_id: str, request: Request, payload: dict = Body(default={}), admin_id: str = Depends(require_perm("money.approve"))):
    try:
        txn = await get_wallet_service().reject_withdrawal(transaction_id, admin_id, payload.get("note", ""))
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail="Transaction not found")
    await _audit(request, admin_id, "withdrawal.reject", target_type="transaction", target_id=transaction_id,
                 summary=f"{abs(txn.amount)} ETB refunded to {txn.player_id}", after={"status": txn.status, "note": payload.get("note", "")})
    return {"status": txn.status}


@router.get("/reconciliation")
async def admin_reconciliation(days: int = 14, admin_id: str = Depends(require_perm("audit.view"))):
    """Daily money screen: wallet integrity checks (same as the 03:00 report) + per-day deposits/withdrawals."""
    from src.services.reconciliation import snapshot

    return await snapshot(days)


async def _export_csv(request: Request, admin_id: str, kind: str, status: Optional[str], start: Optional[str], end: Optional[str]) -> Response:
    from src.services import finance_export as fx

    if status not in (None, "", "all", "approved", "rejected", "pending"):
        raise HTTPException(status_code=400, detail="status must be all, pending, approved or rejected")
    try:
        lo, hi, start_label, end_label = fx.parse_range(start, end)
    except fx.ExportRangeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    txns = await get_wallet_service().transaction_repo.list_for_export(
        kind, lo, hi, None if status in (None, "", "all") else status, limit=fx.MAX_EXPORT_ROWS + 1,
    )
    if len(txns) > fx.MAX_EXPORT_ROWS:
        raise HTTPException(status_code=400, detail=f"More than {fx.MAX_EXPORT_ROWS:,} rows - choose a shorter date range")
    names = {p.id: p.display_name for p in await PlayerRepository().list_by_ids({t.player_id for t in txns})}
    rows = [{
        "id": t.id, "created_at": t.created_at, "resolved_at": t.resolved_at, "status": fx.status_of(t.type),
        "player_id": t.player_id, "player_name": names.get(t.player_id, ""), "amount": abs(t.amount),
        "payment_method": t.payment_method, "payment_verification_url": t.payment_verification_url,
        "legal_name": t.legal_name, "withdrawal_address": t.withdrawal_address,
        "reviewed_by": t.admin_id, "note": t.note,
    } for t in txns]
    await _audit(request, admin_id, f"export.{kind}s", target_type="export", target_id=f"{start_label}..{end_label}",
                 summary=f"{len(rows)} rows, status={status or 'all'}, {start_label} to {end_label}")
    filename = f"melbete_{kind}s_{start_label}_to_{end_label}.csv"
    return Response(
        content=fx.build_csv(rows, kind), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.get("/deposits/export.csv")
async def admin_export_deposits(
    request: Request, status: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None,
    admin_id: str = Depends(require_perm("audit.view")),
):
    return await _export_csv(request, admin_id, "deposit", status, start, end)


@router.get("/withdrawals/export.csv")
async def admin_export_withdrawals(
    request: Request, status: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None,
    admin_id: str = Depends(require_perm("audit.view")),
):
    return await _export_csv(request, admin_id, "withdrawal", status, start, end)


@router.get("/users")
async def admin_list_users(limit: int = 200, search: str = "", admin_id: str = Depends(require_admin_dep)):
    player_repo = PlayerRepository()
    if search.strip():
        rows = await player_repo.search_with_wallets(search.strip(), limit=100)
    else:
        rows = await player_repo.list_all_with_wallets(limit=limit)
    out = [{
        "id": p["id"], "telegram_id": p["telegram_id"],
        "display_name": p["display_name"], "avatar_url": p["avatar_url"], "phone_number": p["phone_number"],
        "payment_name": p["payment_name"],
        "registration_state": p["registration_state"], "balance": p["balance"],
        "won_balance": p["won_balance"], "bonus_balance": p["bonus_balance"],
        "lifetime_deposited": p["lifetime_deposited"], "is_banned": p["is_banned"],
        "ban_reason": p["ban_reason"], "withdrawal_accounts": p["withdrawal_accounts"],
        "auto_bingo_enabled": p["auto_bingo_enabled"], "referred_by": p["referred_by"],
        "referral_count": p["referral_count"], "created_at": p["created_at"].isoformat(),
        "updated_at": p["updated_at"].isoformat(),
    } for p in rows]
    return {"users": out, "total_shown": len(out)}


@router.get("/search")
async def admin_global_search(q: str = "", admin_id: str = Depends(require_admin_dep)):
    """One search box for the whole panel: players (name, Telegram ID, phone, payout name, player ID) and
    deposit / withdrawal records (transaction ID, SMS text, payment link, note, payout account, name)."""
    from src.core.db import get_pool
    from src.services.admin_search import MIN_QUERY_LENGTH, clean_query, like_pattern, phone_pattern

    text = clean_query(q)
    if len(text) < MIN_QUERY_LENGTH:
        return {"query": text, "players": [], "deposits": [], "withdrawals": []}
    like = like_pattern(text)
    phone = phone_pattern(text)
    pool = await get_pool()
    async with pool.acquire() as c:
        players = await c.fetch(
            """
            SELECT p.id, p.display_name, p.telegram_id, p.phone_number, p.payment_name, p.is_banned,
                   COALESCE(w.balance, 0) AS balance, COALESCE(w.bonus_balance, 0) AS bonus_balance
            FROM players p LEFT JOIN wallets w ON w.player_id = p.id
            WHERE p.display_name ILIKE $1 ESCAPE '\\' OR p.telegram_id ILIKE $1 ESCAPE '\\' OR p.id ILIKE $1 ESCAPE '\\'
               OR p.payment_name ILIKE $1 ESCAPE '\\' OR p.phone_number ILIKE $1 ESCAPE '\\'
               OR ($2::text IS NOT NULL AND p.phone_normalized LIKE $2)
            ORDER BY p.created_at DESC LIMIT 8
            """, like, phone)
        txns = await c.fetch(
            """
            SELECT t.id, t.player_id, t.type, t.amount, t.payment_method, t.created_at, t.resolved_at,
                   t.screenshot_url, t.sms_text, t.payment_verification_url, t.admin_id, t.note,
                   t.legal_name, t.withdrawal_address, p.display_name AS player_name
            FROM transactions t LEFT JOIN players p ON p.id = t.player_id
            WHERE t.type IN ('top_up_pending','top_up_approved','top_up_rejected',
                             'withdrawal_pending','withdrawal_approved','withdrawal_rejected')
              AND (t.id ILIKE $1 ESCAPE '\\' OR t.sms_text ILIKE $1 ESCAPE '\\' OR t.payment_verification_url ILIKE $1 ESCAPE '\\'
                   OR t.note ILIKE $1 ESCAPE '\\' OR t.withdrawal_address ILIKE $1 ESCAPE '\\' OR t.legal_name ILIKE $1 ESCAPE '\\')
            ORDER BY t.created_at DESC LIMIT 10
            """, like)
    deposits, withdrawals = [], []
    for t in txns:
        name = t["player_name"] or t["player_id"]
        common = {"id": t["id"], "player_id": t["player_id"], "player_name": name, "payment_method": t["payment_method"],
                  "created_at": t["created_at"].isoformat(), "resolved_at": t["resolved_at"].isoformat() if t["resolved_at"] else None,
                  "type": t["type"], "reviewed_by": t["admin_id"], "note": t["note"]}
        if t["type"].startswith("top_up"):
            status = "approved" if t["type"] == "top_up_approved" else ("rejected" if t["type"] == "top_up_rejected" else "pending")
            deposits.append({**common, "amount": t["amount"], "screenshot_url": t["screenshot_url"], "sms_text": t["sms_text"],
                             "payment_verification_url": t["payment_verification_url"], "status": status})
        else:
            status = "approved" if t["type"] == "withdrawal_approved" else ("rejected" if t["type"] == "withdrawal_rejected" else "pending")
            withdrawals.append({**common, "amount": abs(t["amount"]), "legal_name": t["legal_name"],
                                "withdrawal_address": t["withdrawal_address"], "status": status})
    return {
        "query": text,
        "players": [{"id": p["id"], "display_name": p["display_name"], "telegram_id": p["telegram_id"],
                     "phone_number": p["phone_number"], "payment_name": p["payment_name"], "is_banned": p["is_banned"],
                     "balance": p["balance"], "bonus_balance": p["bonus_balance"]} for p in players],
        "deposits": deposits, "withdrawals": withdrawals,
    }


@router.post("/users/{player_id}/ban")
async def admin_ban_user(
    player_id: str,
    request: Request,
    payload: dict = Body(default={}),
    requester: str = Depends(require_perm("players.ban")),
):
    reason = (payload or {}).get("reason") or "Suspended by admin"
    player_repo = PlayerRepository()
    player = await player_repo.get_by_id(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    await player_repo.set_banned(player_id, True, reason)
    from src.services.agent_rewards import get_agent_reward_service

    await get_agent_reward_service().on_deposit_reversed(player_id)  # no agent reward for a banned player
    logger.warning("Admin %s banned player %s: %s", requester, player_id, reason)
    await _audit(request, requester, "player.ban", target_type="player", target_id=player_id, summary=reason,
                 before={"is_banned": bool(player.is_banned)}, after={"is_banned": True, "reason": reason})
    return {"success": True, "player_id": player_id, "is_banned": True, "ban_reason": reason}


@router.post("/users/{player_id}/unban")
async def admin_unban_user(
    player_id: str,
    request: Request,
    requester: str = Depends(require_perm("players.ban")),
):
    player_repo = PlayerRepository()
    player = await player_repo.get_by_id(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    await player_repo.set_banned(player_id, False)
    logger.warning("Admin %s unbanned player %s", requester, player_id)
    await _audit(request, requester, "player.unban", target_type="player", target_id=player_id,
                 before={"is_banned": bool(player.is_banned)}, after={"is_banned": False})
    return {"success": True, "player_id": player_id, "is_banned": False}


@router.post("/users/{player_id}/reset-balance")
async def admin_reset_user_balance(
    player_id: str,
    request: Request,
    payload: dict = Body(default={}),
    requester: str = Depends(require_perm("players.wallet_adjust")),
):
    from src.repositories.wallet_repository import WalletRepository
    from src.repositories.transaction_repository import TransactionRepository
    from src.models.schemas import Transaction

    wallet_repo = WalletRepository()
    wallet = await wallet_repo.get_by_player(player_id)
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    new_balance = int(payload.get("balance", 0))
    new_won = int(payload.get("won_balance", 0))
    new_bonus = int(payload.get("bonus_balance", 0))
    note = payload.get("note") or f"Admin reset balance: Real={new_balance}, Won={new_won}, Bonus={new_bonus}"

    await wallet_repo.update(player_id, {
        "balance": new_balance,
        "won_balance": new_won,
        "bonus_balance": new_bonus,
    })

    await TransactionRepository().create(
        Transaction(
            player_id=player_id,
            type="admin_adjustment",
            amount=new_balance - wallet.balance,
            balance_after=new_balance,
            status="completed",
            admin_id=requester,
            note=note,
        )
    )

    await _audit(request, requester, "wallet.reset", target_type="player", target_id=player_id, summary=note,
                 before={"balance": wallet.balance, "won_balance": wallet.won_balance, "bonus_balance": wallet.bonus_balance},
                 after={"balance": new_balance, "won_balance": new_won, "bonus_balance": new_bonus})
    return {
        "success": True,
        "player_id": player_id,
        "balance": new_balance,
        "won_balance": new_won,
        "bonus_balance": new_bonus,
    }


@router.get("/referrals")
async def admin_list_referrals(admin_id: str = Depends(require_admin_dep)):
    from src.services.referral_service import ReferralService

    return await ReferralService().list_all_for_admin()


@router.get("/games")
async def admin_list_games(limit: int = 50, state: Optional[str] = None, admin_id: str = Depends(require_admin_dep)):
    rooms = await get_room_service().rooms.list_recent(limit=limit, state_filter=state)
    winner_ids = [w.get("player_id") for room in rooms for w in room.winners if w.get("player_id")]
    players_by_id = {p.id: p for p in await PlayerRepository().list_by_ids(winner_ids)}
    out = []
    for room in rooms:
        winners_detail = []
        for w in room.winners:
            wp = players_by_id.get(w.get("player_id"))
            winners_detail.append({
                "player_id": w.get("player_id"),
                "player_name": wp.display_name if wp else w.get("player_id"),
                "cartela_number": w.get("cartela_number"),
                "payout": w.get("payout"),
                "timestamp": w.get("timestamp"),
            })
        out.append({
            "id": room.id,
            "host_id": room.host_id,
            "variant": room.variant,
            "state": room.state,
            "stake_amount": room.stake_amount,
            "prize_pool": room.prize_pool,
            "players_count": len(room.taken_cartelas),
            "called_numbers_count": len(room.called_numbers),
            "called_numbers": room.called_numbers,
            "draw_pool_remaining": len(room.draw_pool),
            "pattern": room.pattern,
            "cards_per_player": room.cards_per_player,
            "min_players_to_start": room.min_players_to_start,
            "min_cartelas_to_start": getattr(room, "min_cartelas_to_start", 2),
            "countdown_seconds": room.countdown_seconds,
            "countdown_started_at": room.countdown_started_at.isoformat() if room.countdown_started_at else None,
            "draw_interval": room.draw_interval,
            "auto_draw": room.auto_draw,
            "cartela_min": room.cartela_min,
            "cartela_max": room.cartela_max,
            "taken_cartelas": room.taken_cartelas,
            "advanced": room.advanced,
            "winners": winners_detail,
            "end_reason": getattr(room, "end_reason", None),
            "terminated": bool(getattr(room, "end_reason", None)) and "admin_terminated" in str(getattr(room, "end_reason", "")),
            "created_at": room.created_at.isoformat(),
            "updated_at": room.updated_at.isoformat(),
        })
    return {"games": out, "total_shown": len(out)}


@router.post("/games/{room_id}/terminate")
async def admin_terminate_game(
    room_id: str,
    request: Request,
    payload: dict = Body(default={}),
    requester: str = Depends(require_perm("games.manage")),
):
    reason = (payload or {}).get("reason") or "Terminated by admin"
    full_reason = f"admin_terminated: {reason} (by {requester})"

    try:
        room = await get_room_service().get_room(room_id)
    except RoomNotFoundError:
        raise HTTPException(status_code=404, detail="Game not found")

    if room.state != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Only a running game can be terminated (current state: {room.state})",
        )

    ended_room = await get_room_service().end_round_without_winner(room_id, full_reason)

    await manager.broadcast(room_id, {
        "type": "round_terminated",
        "message": "This round was terminated by an admin. Stakes have been refunded.",
        "reason": reason,
    })

    logger.warning("Admin %s terminated running game %s: %s", requester, room_id, reason)
    await _audit(request, requester, "game.terminate", target_type="game", target_id=room_id, summary=reason)

    return {
        "success": True,
        "room_id": ended_room.id,
        "state": ended_room.state,
        "end_reason": ended_room.end_reason,
    }


@router.post("/games/{room_id}/reset-lobby")
async def admin_reset_lobby(
    room_id: str,
    request: Request,
    payload: dict = Body(default={}),
    requester: str = Depends(require_perm("games.manage")),
):
    """Un-stick a waiting lobby: drop every cartela reservation and stop the countdown.

    Nothing is charged while a lobby is waiting (stakes are taken when the round starts), so no refunds
    are needed. Players stay in the same room and can pick cartelas again."""
    reason = ((payload or {}).get("reason") or "").strip() or "Lobby reset by admin"
    svc = get_room_service()
    try:
        await svc.get_room(room_id)
    except RoomNotFoundError:
        raise HTTPException(status_code=404, detail="Game not found")

    async with svc._exclusive_room(room_id):
        room = await svc.get_room(room_id)
        if room.state != "waiting":
            raise HTTPException(
                status_code=400,
                detail=f"Only a waiting lobby can be reset (current state: {room.state}). "
                       "Use Terminate for a running round.",
            )
        released = len(room.taken_cartelas or [])
        reset_room = await svc.cancel_round(room_id)

    await manager.broadcast(room_id, {
        "type": "round_cancelled",
        "message": "This lobby was reset by an admin. Please pick your cartela again.",
    })
    logger.warning("Admin %s reset waiting lobby %s (%d cartelas released): %s", requester, room_id, released, reason)
    await _audit(request, requester, "game.reset_lobby", target_type="game", target_id=room_id,
                 summary=f"{reason} ({released} cartela(s) released)")
    return {"success": True, "room_id": reset_room.id, "state": reset_room.state, "released_cartelas": released}


@router.post("/games/{room_id}/force-start")
async def admin_force_start_game(
    room_id: str,
    request: Request,
    payload: dict = Body(default={}),
    requester: str = Depends(require_perm("games.manage")),
):
    """Start a waiting lobby now instead of waiting for its countdown.

    Uses the normal start path: every cartela is charged, anyone who cannot pay is dropped, and the
    lobby's minimum-cartelas rule still applies."""
    reason = ((payload or {}).get("reason") or "").strip() or "Started early by admin"
    svc = get_room_service()
    try:
        room = await svc.get_room(room_id)
    except RoomNotFoundError:
        raise HTTPException(status_code=404, detail="Game not found")
    if room.state != "waiting":
        raise HTTPException(status_code=400, detail=f"Only a waiting lobby can be started (current state: {room.state})")

    from src.api.main import start_round_and_announce

    try:
        started, dropped = await start_round_and_announce(room_id)
    except InvalidStateError:
        raise HTTPException(status_code=409, detail="The round already started")
    if started is None:
        if dropped:
            await _audit(request, requester, "game.force_start", target_type="game", target_id=room_id,
                         summary=f"{reason} - nobody could be charged, lobby reset")
            raise HTTPException(status_code=409, detail="Nobody could be charged, so the lobby was reset")
        needed = int(getattr(room, "min_cartelas_to_start", 2) or 2)
        raise HTTPException(
            status_code=400,
            detail=f"Not enough cartelas to start: {len(room.taken_cartelas or [])} held, {needed} needed",
        )

    logger.warning("Admin %s force-started room %s: %s", requester, room_id, reason)
    await _audit(request, requester, "game.force_start", target_type="game", target_id=room_id,
                 summary=f"{reason} ({len(started.taken_cartelas)} cartela(s), {len(dropped)} dropped)")
    return {
        "success": True,
        "room_id": started.id,
        "state": started.state,
        "cartelas": len(started.taken_cartelas),
        "prize_pool": started.prize_pool,
        "dropped_players": len(dropped),
    }


@router.delete("/games/{room_id}")
async def admin_delete_game(
    room_id: str,
    request: Request,
    requester: str = Depends(require_perm("games.manage")),
):
    try:
        room = await get_room_service().get_room(room_id)
    except RoomNotFoundError:
        raise HTTPException(status_code=404, detail="Game not found")

    if room.state != "waiting":
        raise HTTPException(
            status_code=400,
            detail=f"Only a waiting game room can be deleted (current state: {room.state})",
        )

    await get_room_service().rooms._execute("DELETE FROM cards WHERE room_id = $1", room_id)
    await get_room_service().rooms._execute("DELETE FROM game_rooms WHERE id = $1", room_id)

    logger.warning("Admin %s deleted waiting room %s", requester, room_id)
    await _audit(request, requester, "game.delete", target_type="game", target_id=room_id, summary="Deleted waiting room")
    return {"success": True, "message": "Waiting room deleted successfully."}


@router.get("/report")
async def admin_report(admin_id: str = Depends(require_admin_dep)):
    room_service = get_room_service()
    tracks = {}
    for t in RoomService.TRACKS:
        r = await room_service.get_or_create_waiting_session(t)
        tracks[t] = {
            "room_id": r.id,
            "room_state": r.state,
            "players_in_round": len(r.taken_cartelas),
            "prize_pool": r.prize_pool,
            "called_numbers": len(r.called_numbers),
        }
    room = await room_service.get_or_create_waiting_session("real_10")
    pending_deposits = await get_wallet_service().list_pending_topups()
    pending_withdrawals = await get_wallet_service().list_pending_withdrawals()
    recent_games = await get_room_service().rooms.list_recent(limit=200)
    finished_games = [r for r in recent_games if r.state == "ended"]
    total_paid_out = sum(w.get("payout", 0) for r in finished_games for w in r.winners)
    recent_list = []
    for r in recent_games[:10]:
        recent_list.append({
            "id": r.id,
            "track": getattr(r, "track", "real_10"),
            "state": r.state,
            "prize_pool": r.prize_pool or 0,
            "players_count": len(r.taken_cartelas),
            "called_numbers_count": len(r.called_numbers),
            "stake_amount": r.stake_amount or 0,
            "winners_count": len(r.winners),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {
        "tracks": tracks,
        "room_id": room.id,
        "room_state": room.state,
        "players_in_round": len(room.taken_cartelas),
        "prize_pool": room.prize_pool,
        "called_numbers": len(room.called_numbers),
        "pending_deposits": len(pending_deposits),
        "pending_withdrawals": len(pending_withdrawals),
        "registered_users": await PlayerRepository().count(),
        "games_played_recent": len(finished_games),
        "total_paid_out_recent": total_paid_out,
        "recent_games_list": recent_list,
    }


@router.get("/economy")
async def admin_economy_summary(admin_id: str = Depends(require_admin_dep)):
    from src.repositories.transaction_repository import TransactionRepository as TR
    from src.repositories.player_repository import PlayerRepository as PR
    txn_repo = TR()
    pr_repo = PR()
    summary, time_series, top_depositors, top_winners, wallet_totals = await asyncio.gather(
        txn_repo.get_economy_summary(),
        txn_repo.get_economy_time_series(days=30),
        txn_repo.get_top_depositors(limit=10),
        txn_repo.get_top_winners(limit=10),
        pr_repo.get_wallet_totals(),
    )
    all_ids = list({r["player_id"] for r in top_depositors + top_winners})
    players_by_id = {p.id: p for p in await PR().list_by_ids(all_ids)} if all_ids else {}
    def _name(player_id):
        p = players_by_id.get(player_id)
        return p.display_name if p else player_id

    depositors_out = [
        {
            "player_id": r["player_id"],
            "display_name": _name(r["player_id"]),
            "telegram_id": players_by_id.get(r["player_id"]).telegram_id if players_by_id.get(r["player_id"]) else r["player_id"],
            "total_deposited": r["total_deposited"],
            "deposit_count": r["deposit_count"],
            "last_deposit_at": r["last_deposit_at"].isoformat() if r.get("last_deposit_at") else None,
        }
        for r in top_depositors
    ]
    winners_out = [
        {
            "player_id": r["player_id"],
            "display_name": _name(r["player_id"]),
            "telegram_id": players_by_id.get(r["player_id"]).telegram_id if players_by_id.get(r["player_id"]) else r["player_id"],
            "total_won": r["total_won"],
            "win_count": r["win_count"],
            "last_win_at": r["last_win_at"].isoformat() if r.get("last_win_at") else None,
        }
        for r in top_winners
    ]
    stakes = int(summary.get("total_stakes_collected", 0))
    payouts = int(summary.get("total_payouts", 0))
    refunds = int(summary.get("total_refunds", 0))
    net_margin = stakes - payouts - refunds
    time_series_out = [
        {
            "day": str(r["day"]),
            "deposited": int(r["deposited"]),
            "withdrawn": int(r["withdrawn"]),
            "stakes": int(r["stakes"]),
            "payouts": int(r["payouts"]),
            "refunds": int(r["refunds"]),
            "net": int(r["stakes"]) - int(r["payouts"]) - int(r["refunds"]),
        }
        for r in time_series
    ]
    return {
        "summary": {
            "total_deposited": int(summary.get("total_deposited", 0)),
            "total_withdrawn": int(summary.get("total_withdrawn", 0)),
            "total_stakes_collected": stakes,
            "total_payouts": payouts,
            "total_refunds": refunds,
            "net_house_margin": net_margin,
            "total_free_credit_issued": int(summary.get("total_free_credit_issued", 0)),
            "total_referral_bonuses": int(summary.get("total_referral_bonuses", 0)),
            "total_admin_adjustments": int(summary.get("total_admin_adjustments", 0)),
            "active_wallets": int(summary.get("active_wallets", 0)),
            "deposit_count": int(summary.get("deposit_count", 0)),
            "withdrawal_count": int(summary.get("withdrawal_count", 0)),
            "stake_count": int(summary.get("stake_count", 0)),
            "payout_count": int(summary.get("payout_count", 0)),
            "total_real_balance": int(wallet_totals.get("total_real_balance", 0)),
            "total_bonus_balance": int(wallet_totals.get("total_bonus_balance", 0)),
            "total_won_balance": int(wallet_totals.get("total_won_balance", 0)),
        },
        "time_series": time_series_out,
        "top_depositors": depositors_out,
        "top_winners": winners_out,
    }


@router.get("/economy/user/{player_id}")
async def admin_economy_user(player_id: str, admin_id: str = Depends(require_admin_dep)):
    from src.repositories.transaction_repository import TransactionRepository as TR
    txn_repo = TR()
    player = await PlayerRepository().get_by_id(player_id)
    if not player:
        player = await PlayerRepository().get_by_telegram_id(player_id.removeprefix("tg_"))
    transactions = await txn_repo.list_by_player_full(player.id if player else player_id)
    return {
        "player_id": player.id if player else player_id,
        "display_name": player.display_name if player else player_id,
        "telegram_id": player.telegram_id if player else None,
        "transactions": [
            {
                "id": str(t["id"]),
                "type": t["type"],
                "amount": t["amount"],
                "balance_after": t.get("balance_after"),
                "status": t.get("status"),
                "payment_method": t.get("payment_method"),
                "room_id": t.get("room_id"),
                "note": t.get("note"),
                "created_at": t["created_at"].isoformat() if t.get("created_at") else None,
                "resolved_at": t["resolved_at"].isoformat() if t.get("resolved_at") else None,
            }
            for t in transactions
        ],
    }


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Agents (resellers) - super admin only, because they move money
# ---------------------------------------------------------------------------

def _agent_error(e: AgentError) -> HTTPException:
    if isinstance(e, AgentNotFoundError):
        return HTTPException(status_code=404, detail=e.message)
    if isinstance(e, AgentAlreadyExistsError):
        return HTTPException(status_code=409, detail=e.message)
    return HTTPException(status_code=400, detail=e.message)


@router.get("/agents")
async def admin_list_agents(requester: str = Depends(require_super_admin_dep)):
    service = get_agent_service()
    agents = await service.list_agents()
    out = []
    for a in agents:
        d = a.model_dump(mode="json")
        d["player_count"] = await service.agents.count_players(a.id)
        d["commission_percent"] = a.commission_bps / 100
        out.append(d)
    return {"agents": out}


def _reward_error(e) -> HTTPException:
    return HTTPException(status_code=400, detail=e.message)


@router.get("/abuse")
async def admin_abuse_report(min_players: int = 3, requester: str = Depends(require_super_admin_dep)):
    """Accounts that look related: same connection, same phone number, same payout account.
    Signals for a human to judge - families share Wi-Fi - nothing is banned automatically."""
    from src.services.abuse_signals import build_report

    return await build_report(max(2, min(int(min_players), 20)))


@router.get("/agent-rewards")
async def admin_agent_rewards(requester: str = Depends(require_super_admin_dep)):
    """Reward settings, tiers, the latest rewards (pending/paid/cancelled) and abuse flags."""
    from src.services.agent_rewards import get_agent_reward_service

    return await get_agent_reward_service().admin_overview()


@router.put("/agent-rewards/settings")
async def admin_update_agent_reward_settings(request: Request, payload: dict = Body(...), requester: str = Depends(require_super_admin_dep)):
    from src.services.agent_rewards import RewardInputError, get_agent_reward_service

    try:
        settings_out = await get_agent_reward_service().update_settings(requester, payload)
    except RewardInputError as e:
        raise _reward_error(e)
    await _audit(request, requester, "agent_rewards.settings", target_type="config", target_id="agent_rewards", summary="Updated agent reward settings", after=payload)
    return {"settings": settings_out}


@router.put("/agent-rewards/tiers")
async def admin_update_agent_tiers(request: Request, payload: dict = Body(...), requester: str = Depends(require_super_admin_dep)):
    """Body: {"tiers": [{"label": "Silver", "min_monthly_volume": 50000, "commission_percent": 4}, ...]}"""
    from src.services.agent_rewards import RewardInputError, get_agent_reward_service

    try:
        tiers = await get_agent_reward_service().set_tiers(requester, payload.get("tiers"))
    except RewardInputError as e:
        raise _reward_error(e)
    await _audit(request, requester, "agent_rewards.tiers", target_type="config", target_id="agent_rewards", summary=f"Set {len(tiers)} reward tiers")
    return {"tiers": [{"label": t["label"], "min_monthly_volume": t["min_monthly_volume"],
                       "commission_percent": t["commission_bps"] / 100} for t in tiers]}


@router.post("/agent-rewards/{reward_id}/cancel")
async def admin_cancel_agent_reward(reward_id: str, request: Request, requester: str = Depends(require_super_admin_dep)):
    from src.services.agent_rewards import RewardInputError, get_agent_reward_service

    try:
        await get_agent_reward_service().cancel_reward(requester, reward_id)
    except RewardInputError as e:
        raise _reward_error(e)
    await _audit(request, requester, "agent_rewards.cancel", target_type="agent_reward", target_id=reward_id, summary="Cancelled agent reward")
    return {"success": True}


@router.get("/agents/{agent_id}/ledger")
async def admin_agent_ledger(agent_id: str, requester: str = Depends(require_super_admin_dep)):
    try:
        entries = await get_agent_service().ledger_for_admin(agent_id)
    except AgentError as e:
        raise _agent_error(e)
    return {"ledger": [e.model_dump(mode="json") for e in entries]}


def _application_json(a: dict) -> dict:
    out = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(a).items()}
    out["telegram_id"] = out.get("telegram_id") or str(out.get("player_id", "")).replace("tg_", "")
    return out


@router.get("/agent-applications")
async def admin_list_agent_applications(status: str = "pending", requester: str = Depends(require_super_admin_dep)):
    """Players asking to become agents. status = pending (default) | approved | rejected | all."""
    try:
        rows = await get_agent_service().list_applications(None if status == "all" else status)
    except AgentError as e:
        raise _agent_error(e)
    return {"applications": [_application_json(r) for r in rows]}


@router.post("/agent-applications/{application_id}/approve")
async def admin_approve_agent_application(
    application_id: str, request: Request, payload: dict = Body(default={}),
    requester: str = Depends(require_super_admin_dep),
):
    """Body: {"commission_percent": 3, "note": "optional"} - turns the player into an active agent."""
    try:
        res = await get_agent_service().review_application(
            requester, application_id, True, (payload or {}).get("commission_percent"), (payload or {}).get("note", ""),
        )
    except AgentError as e:
        raise _agent_error(e)
    agent = res["agent"]
    await _audit(request, requester, "agent.application_approve", target_type="agent_application", target_id=application_id,
                 summary=f"Approved agent request from {res['application']['player_id']}",
                 after={"commission_percent": (agent.commission_bps / 100) if agent else None})
    return {"application": _application_json(res["application"]), "agent": agent.model_dump(mode="json") if agent else None}


@router.post("/agent-applications/{application_id}/reject")
async def admin_reject_agent_application(
    application_id: str, request: Request, payload: dict = Body(default={}),
    requester: str = Depends(require_super_admin_dep),
):
    try:
        res = await get_agent_service().review_application(requester, application_id, False, None, (payload or {}).get("note", ""))
    except AgentError as e:
        raise _agent_error(e)
    await _audit(request, requester, "agent.application_reject", target_type="agent_application", target_id=application_id,
                 summary=f"Rejected agent request from {res['application']['player_id']}", after={"note": (payload or {}).get("note", "")})
    return {"application": _application_json(res["application"])}


@router.put("/agents/{agent_id}/commission")
async def admin_set_agent_commission(
    agent_id: str, request: Request, payload: dict = Body(...), requester: str = Depends(require_super_admin_dep),
):
    """Body: {"commission_percent": 5} - the agent's base rate (tiers can still raise what is paid)."""
    service = get_agent_service()
    try:
        before = await service.get_agent(agent_id)
        agent = await service.update_commission(requester, agent_id, payload.get("commission_percent"))
    except AgentError as e:
        raise _agent_error(e)
    await _audit(request, requester, "agent.commission", target_type="agent", target_id=agent_id,
                 summary=f"Commission {before.commission_bps / 100:g}% -> {agent.commission_bps / 100:g}%",
                 before={"commission_percent": before.commission_bps / 100}, after={"commission_percent": agent.commission_bps / 100})
    return {"agent": agent.model_dump(mode="json")}


@router.put("/agents/{agent_id}/rewards")
async def admin_set_agent_rewards(
    agent_id: str, request: Request, payload: dict = Body(...), requester: str = Depends(require_super_admin_dep),
):
    """Body: {"enabled": false} - stop (or resume) new-player / retention rewards for this agent only."""
    try:
        agent = await get_agent_service().set_rewards_enabled(requester, agent_id, payload.get("enabled"))
    except AgentError as e:
        raise _agent_error(e)
    await _audit(request, requester, "agent.rewards_toggle", target_type="agent", target_id=agent_id,
                 summary=f"Rewards {'enabled' if agent.rewards_enabled else 'disabled'}", after={"rewards_enabled": agent.rewards_enabled})
    return {"agent": agent.model_dump(mode="json")}


@router.post("/agents")
async def admin_create_agent(request: Request, payload: dict = Body(...), requester: str = Depends(require_super_admin_dep)):
    """Body: {"telegram_id": "123456789", "commission_percent": 5}"""
    try:
        agent = await get_agent_service().create_agent(
            requester, payload.get("telegram_id"), payload.get("commission_percent", 0)
        )
    except AgentError as e:
        raise _agent_error(e)
    await _audit(request, requester, "agent.create", target_type="agent", target_id=str(getattr(agent, "id", "")),
                 summary=f"Agent {payload.get('telegram_id')} at {payload.get('commission_percent', 0)}%")
    return {"agent": agent.model_dump(mode="json")}


@router.post("/agents/{agent_id}/suspend")
async def admin_suspend_agent(agent_id: str, request: Request, requester: str = Depends(require_super_admin_dep)):
    try:
        agent = await get_agent_service().set_status(requester, agent_id, "suspended")
    except AgentError as e:
        raise _agent_error(e)
    await _audit(request, requester, "agent.suspend", target_type="agent", target_id=agent_id, summary="Suspended agent")
    return {"agent": agent.model_dump(mode="json")}


@router.post("/agents/{agent_id}/activate")
async def admin_activate_agent(agent_id: str, request: Request, requester: str = Depends(require_super_admin_dep)):
    try:
        agent = await get_agent_service().set_status(requester, agent_id, "active")
    except AgentError as e:
        raise _agent_error(e)
    await _audit(request, requester, "agent.activate", target_type="agent", target_id=agent_id, summary="Activated agent")
    return {"agent": agent.model_dump(mode="json")}


@router.post("/agents/{agent_id}/float")
async def admin_adjust_agent_float(
    agent_id: str,
    request: Request,
    payload: dict = Body(...),
    requester: str = Depends(require_super_admin_dep),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Body: {"amount": 5000, "note": "Telebirr ref ..."}  - positive funds the agent, negative takes
    float back. Send an Idempotency-Key header so a double click or retry cannot apply it twice."""

    async def _apply() -> dict:
        try:
            agent = await get_agent_service().adjust_float(
                requester, agent_id, payload.get("amount"), str(payload.get("note") or "")
            )
        except AgentError as e:
            raise _agent_error(e)
        await _audit(request, requester, "agent.float", target_type="agent", target_id=agent_id,
                     summary=f"{payload.get('amount')} ETB: {str(payload.get('note') or '')[:200]}", after={"amount": payload.get("amount")})
        return {"agent": agent.model_dump(mode="json")}

    return await run_idempotent(requester, f"agent_float:{agent_id}", idempotency_key, payload, _apply)
