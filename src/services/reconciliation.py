"""Daily wallet reconciliation. READ-ONLY: it reports problems to the admin chat and the log, and never
changes a balance. Runs once a day (default 03:00 Ethiopia time) on whichever instance holds the lease."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from src.core import loop_lock
from src.core.config import settings
from src.core.db import get_pool
from src.repositories.transaction_repository import TransactionRepository
from src.services.reconciliation_report import EAT, format_report, persistent, seconds_until

logger = logging.getLogger(__name__)
RECON_LOCK = "reconcile"


async def collect() -> dict:
    pool = await get_pool()
    rows = lambda rs: [dict(r) for r in rs]  # noqa: E731
    negative = rows(await pool.fetch(
        "SELECT player_id, balance, won_balance, bonus_balance FROM wallets "
        "WHERE balance < 0 OR won_balance < 0 OR bonus_balance < 0 ORDER BY player_id LIMIT 20"))
    won_over = rows(await pool.fetch(
        "SELECT player_id, balance, won_balance FROM wallets WHERE won_balance > balance ORDER BY player_id LIMIT 20"))
    deposit_ledger = rows(await pool.fetch(
        """SELECT w.player_id, w.lifetime_deposited AS wallet_total, COALESCE(t.total, 0) AS ledger_total
           FROM wallets w LEFT JOIN (SELECT player_id, SUM(amount) AS total FROM transactions
                                     WHERE type = 'top_up_approved' GROUP BY player_id) t ON t.player_id = w.player_id
           WHERE w.lifetime_deposited <> COALESCE(t.total, 0)
           ORDER BY ABS(w.lifetime_deposited - COALESCE(t.total, 0)) DESC LIMIT 20"""))
    totals = dict(await pool.fetchrow(
        "SELECT COALESCE(SUM(balance), 0) AS real_total, COALESCE(SUM(won_balance), 0) AS withdrawable, "
        "COALESCE(SUM(bonus_balance), 0) AS bonus_total, COUNT(*) AS wallets FROM wallets"))
    pending = dict(await pool.fetchrow(
        """SELECT COUNT(*) FILTER (WHERE type = 'top_up_pending') AS topups,
                  COUNT(*) FILTER (WHERE type = 'withdrawal_pending') AS withdrawals,
                  COALESCE(SUM(ABS(amount)) FILTER (WHERE type = 'withdrawal_pending'), 0) AS withdrawal_amount,
                  COUNT(*) FILTER (WHERE created_at < now() - interval '24 hours') AS stale
           FROM transactions WHERE status = 'pending'"""))
    return {"negative": negative, "won_over_balance": won_over, "deposit_ledger": deposit_ledger,
            "totals": totals, "pending": pending}


async def snapshot(days: int = 14) -> dict:
    """Live read for the admin Reconciliation screen: the same integrity checks as the 03:00 job plus a
    per-day deposit/withdrawal table. Read-only. Unlike run_once() it does NOT wait 60s and re-read to
    drop transactions that were in flight, so a finding here is a prompt to look, not proof."""
    days = max(1, min(int(days), 90))
    today = datetime.now(EAT).date()
    since = datetime(today.year, today.month, today.day, tzinfo=EAT).astimezone(timezone.utc) - timedelta(days=days - 1)
    report = await collect()
    daily = await TransactionRepository().daily_money_summary(since)
    for d in daily:
        d["day"] = d["day"].isoformat()
        d["net_cash_etb"] = int(d["deposits_approved_etb"]) - int(d["withdrawals_paid_etb"])
    problems = bool(report["negative"] or report["won_over_balance"])
    return {
        "generated_at": datetime.now(EAT).strftime("%Y-%m-%d %H:%M:%S"),
        "today": today.isoformat(),
        "days": days,
        "ok": not problems,
        "needs_review": bool(report["deposit_ledger"]),
        "report": report,
        "daily": daily,
        "text": format_report(report, today.strftime("%d %b %Y"))[0],
    }


async def run_once() -> tuple[str, bool]:
    first = await collect()
    if first["negative"] or first["won_over_balance"] or first["deposit_ledger"]:
        await asyncio.sleep(60)               # confirm: ignore anything that was only a transaction in flight
        second = await collect()
        for k in ("negative", "won_over_balance", "deposit_ledger"):
            second[k] = persistent(first[k], second[k]) if first[k] else []
        first = {**second}
    text, problems = format_report(first, datetime.now(EAT).strftime("%d %b %Y"))
    (logger.error if problems else logger.info)("Reconciliation:\n%s", text)
    await _alert(text)
    return text, problems


async def _alert(text: str) -> None:
    if not settings.admin_chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                              json={"chat_id": settings.admin_chat_id, "text": text})
    except Exception:  # noqa: BLE001
        logger.warning("Could not send the reconciliation report to the admin chat", exc_info=True)


async def reconciliation_loop() -> None:
    hour = settings.reconciliation_hour_eat
    if hour < 0:
        return
    while True:
        await asyncio.sleep(seconds_until(hour))
        try:
            if await loop_lock.try_acquire(RECON_LOCK, 3600):   # only one instance runs it
                await run_once()
        except Exception:  # noqa: BLE001
            logger.exception("Daily reconciliation failed")
        await asyncio.sleep(60)
