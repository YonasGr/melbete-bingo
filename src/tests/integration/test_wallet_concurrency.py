"""Concurrency tests against real Postgres: money must be conserved under parallel requests.

Each test fires many simultaneous operations at the same wallet and asserts the invariant
that an in-memory fake cannot prove (row locks / guarded UPDATEs actually serialize them).
"""
import asyncio

import asyncpg
import pytest

from src.api.idempotency import run_idempotent
from src.models.schemas import Transaction, Wallet
from src.repositories.idempotency_repository import IdempotencyRepository
from src.repositories.settings_repository import SettingsRepository
from src.repositories.transaction_repository import TransactionRepository
from src.repositories.wallet_repository import WalletRepository, WithdrawalRejected
from src.services.wallet_service import DuplicateTransactionError, WalletService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
N = 25


def _service():
    return WalletService(
        wallet_repo=WalletRepository(), transaction_repo=TransactionRepository(), settings_repo=SettingsRepository()
    )


async def _seed(player, balance=0, won=0, bonus=0, lifetime=0):
    await WalletRepository().create(
        Wallet(player_id=player, balance=balance, won_balance=won, bonus_balance=bonus, lifetime_deposited=lifetime)
    )


async def _wallet(pool, player):
    return await pool.fetchrow("SELECT * FROM wallets WHERE player_id = $1", player)


async def test_concurrent_withdrawals_cannot_double_spend_winnings(pg):
    await _seed("p1", balance=1000, won=300, lifetime=500)
    repo = WalletRepository()

    async def attempt():
        try:
            return await repo.hold_withdrawal_atomic(
                Transaction(player_id="p1", type="withdrawal_pending", amount=-300, status="pending")
            )
        except WithdrawalRejected:
            return None

    results = await asyncio.gather(*[attempt() for _ in range(N)])
    assert sum(r is not None for r in results) == 1  # exactly one hold of the 300 won
    w = await _wallet(pg, "p1")
    assert (w["balance"], w["won_balance"]) == (700, 0)
    held = await pg.fetchval("SELECT count(*) FROM transactions WHERE player_id='p1' AND type='withdrawal_pending'")
    assert held == 1


async def test_concurrent_partial_withdrawals_never_exceed_won_balance(pg):
    await _seed("p2", balance=2000, won=500, lifetime=500)
    repo = WalletRepository()

    async def attempt():
        try:
            return await repo.hold_withdrawal_atomic(
                Transaction(player_id="p2", type="withdrawal_pending", amount=-200, status="pending")
            )
        except WithdrawalRejected:
            return None

    ok = [r for r in await asyncio.gather(*[attempt() for _ in range(N)]) if r]
    assert len(ok) == 2  # 500 // 200
    w = await _wallet(pg, "p2")
    assert w["won_balance"] == 100 and w["balance"] == 1600 and w["won_balance"] >= 0


async def test_withdrawal_requires_a_deposit(pg):
    await _seed("p3", balance=500, won=500, lifetime=0)
    with pytest.raises(WithdrawalRejected):
        await WalletRepository().hold_withdrawal_atomic(
            Transaction(player_id="p3", type="withdrawal_pending", amount=-200, status="pending")
        )


async def test_concurrent_stakes_never_overspend(pg):
    await _seed("p4", balance=100, bonus=0)
    repo = WalletRepository()
    results = await asyncio.gather(*[repo.debit_stake_atomic("p4", 10, "any") for _ in range(N)])
    assert sum(r is not None for r in results) == 10
    assert (await _wallet(pg, "p4"))["balance"] == 0


async def test_free_credit_is_granted_exactly_once(pg):
    svc = _service()
    results = await asyncio.gather(*[svc.grant_free_credit_if_eligible("newbie") for _ in range(N)])
    assert sum(r is not None for r in results) == 1
    w = await _wallet(pg, "newbie")
    assert w["bonus_balance"] == 50 and w["free_credit_granted"] is True
    assert await pg.fetchval("SELECT count(*) FROM transactions WHERE player_id='newbie' AND type='free_credit'") == 1


async def test_concurrent_topup_approvals_credit_once(pg):
    txn = await TransactionRepository().create(
        Transaction(player_id="p5", type="top_up_pending", amount=400, status="pending")
    )
    repo = WalletRepository()
    results = await asyncio.gather(*[repo.approve_topup_atomic(txn.id, f"admin{i}") for i in range(N)])
    assert sum(r.applied for r in results) == 1
    w = await _wallet(pg, "p5")
    assert (w["balance"], w["lifetime_deposited"]) == (400, 400)


async def test_duplicate_payment_url_blocked_by_database(pg):
    svc = _service()
    url = "https://pay.example/receipt/ABC123"

    async def submit():
        try:
            return await svc.request_deposit("p6", 100, "telebirr", payment_verification_url=url, sms_text="x")
        except DuplicateTransactionError:
            return None

    results = await asyncio.gather(*[submit() for _ in range(N)])
    assert sum(r is not None for r in results) == 1
    assert await pg.fetchval("SELECT count(*) FROM transactions WHERE payment_verification_url=$1", url) == 1


async def test_idempotent_request_executes_once_under_concurrency(pg):
    runs = []

    async def handler():
        runs.append(1)
        await asyncio.sleep(0.05)
        return {"transaction_id": "t-1"}

    async def call():
        try:
            return await run_idempotent("p7", "withdraw", "withdraw-key-0001", {"amount": 200}, handler)
        except Exception as e:  # 409 for the in-flight losers
            return e

    results = await asyncio.gather(*[call() for _ in range(N)])
    assert len(runs) == 1
    assert sum(r == {"transaction_id": "t-1"} for r in results) == 1
    # after completion, every retry replays the stored response without running the handler
    assert await call() == {"transaction_id": "t-1"}
    assert len(runs) == 1


async def test_stale_in_progress_key_can_be_taken_over(pg):
    repo = IdempotencyRepository()
    assert await repo.begin("p8", "withdraw", "withdraw-key-0002", "h", 120) is None
    assert (await repo.begin("p8", "withdraw", "withdraw-key-0002", "h", 120))["status"] == "in_progress"
    await pg.execute("UPDATE idempotency_keys SET updated_at = now() - interval '10 minutes'")
    assert await repo.begin("p8", "withdraw", "withdraw-key-0002", "h", 120) is None


async def test_concurrent_reject_and_approve_resolve_a_withdrawal_once(pg):
    await _seed("p10", balance=1000, won=300, lifetime=500)
    repo = WalletRepository()
    txn = await repo.hold_withdrawal_atomic(
        Transaction(player_id="p10", type="withdrawal_pending", amount=-300, status="pending")
    )
    calls = [repo.resolve_withdrawal_atomic(txn.id, f"a{i}", "reject" if i % 2 else "approve") for i in range(N)]
    results = await asyncio.gather(*calls)
    assert sum(applied for _, applied in results) == 1
    w = await _wallet(pg, "p10")
    final = await pg.fetchval("SELECT type FROM transactions WHERE id = $1", txn.id)
    # either paid (balance stays debited) or refunded exactly once - never both, never twice
    assert (w["balance"], w["won_balance"]) == ((700, 0) if final == "withdrawal_approved" else (1000, 300))
