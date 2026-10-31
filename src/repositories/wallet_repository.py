"""Postgres repository for player wallets."""
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from src.models.schemas import Transaction, Wallet, new_id, utcnow
from src.repositories.base import BaseRepository
from src.repositories.transaction_repository import _COLUMNS as _TXN_COLUMNS
from src.services.stake_math import StakeSplit, plan_stake_debit

logger = logging.getLogger(__name__)


class WithdrawalRejected(Exception):
    """Raised by `hold_withdrawal_atomic` when the locked wallet cannot cover the hold."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # "no_deposit" | "insufficient_won"


@dataclass
class TopupApproval:
    """Outcome of `WalletRepository.approve_topup_atomic`."""

    txn: Optional[Transaction]  # None => no such transaction
    applied: bool  # True only for the one call that actually credited the wallet
    extra: Any = None  # whatever the first-deposit hook returned (None if it did not run)
    approved_extra: Any = None  # whatever the on_approved hook returned (e.g. agent commission)


class WalletRepository(BaseRepository):
    table_name = "wallets"

    async def get_by_player(self, player_id: str) -> Optional[Wallet]:
        row = await self._fetchrow("SELECT * FROM wallets WHERE player_id = $1", player_id)
        return Wallet(**row) if row else None

    async def create(self, wallet: Wallet) -> Wallet:
        data = wallet.model_dump()
        await self._execute(
            """
            INSERT INTO wallets
                (id, player_id, balance, won_balance, bonus_balance,
                 lifetime_deposited, free_credit_granted, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (player_id) DO NOTHING
            """,
            data["id"], data["player_id"], data["balance"], data["won_balance"],
            data["bonus_balance"], data["lifetime_deposited"], data["free_credit_granted"],
            data["created_at"], data["updated_at"],
        )
        return wallet

    async def update(self, player_id: str, fields: dict) -> None:
        fields = {**fields, "updated_at": utcnow()}
        await self._update_fields("wallets", "player_id", player_id, fields)

    async def atomic_debit_real(self, player_id: str, amount: int) -> Optional[Wallet]:
        """Atomically debits `amount` from `balance` and preserves `won_balance`.
        Returns the updated Wallet, or None if `balance < amount` (insufficient funds).
        PostgreSQL's single-statement row lock guarantees atomic execution even under high concurrency."""
        query = """
            UPDATE wallets
            SET balance = balance - $2,
                won_balance = LEAST(won_balance, balance - $2),
                updated_at = $3
            WHERE player_id = $1 AND balance >= $2
            RETURNING *;
        """
        row = await self._fetchrow(query, player_id, amount, utcnow())
        return Wallet(**row) if row else None

    async def approve_topup_atomic(
        self,
        transaction_id: str,
        admin_id: str,
        on_first_deposit: Optional[Callable[[Any, Transaction], Awaitable[Any]]] = None,
        on_approved: Optional[Callable[[Any, Transaction], Awaitable[Any]]] = None,
    ) -> TopupApproval:
        """Approve a pending deposit and credit the wallet in ONE database transaction.

        Row locks make concurrent approvals (web panel + Telegram bot, two admins,
        double clicks) safe: the second caller blocks, then sees the deposit is
        already resolved and gets `applied=False`, so the wallet is credited once.

        `on_first_deposit(conn, txn)` runs inside the same transaction when this is
        the player's first-ever deposit (decided under the wallet row lock), so any
        reward it grants commits or rolls back together with the cash credit. The
        hook owns its own error policy; an exception it lets escape rolls the whole
        approval back.

        `on_approved(conn, txn)` runs inside the same transaction for EVERY approved deposit (used for
        referral-agent commission); it owns its own error policy, like on_first_deposit.

        Lock order, shared with BonusService: bonus-campaign advisory lock ->
        transactions row -> wallets row. Keep it consistent to avoid deadlocks.
        """
        pool = await self._pool()
        async with pool.acquire() as c:
            async with c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock(hashtext('melbete_bonus_campaign'))")
                row = await c.fetchrow("SELECT * FROM transactions WHERE id = $1 FOR UPDATE", transaction_id)
                if row is None:
                    return TopupApproval(None, False)
                txn = Transaction(**dict(row))
                if txn.type != "top_up_pending" or txn.status != "pending":
                    return TopupApproval(txn, False)  # already resolved, or not a deposit

                await c.execute(
                    "INSERT INTO wallets (id, player_id) VALUES ($1, $2) ON CONFLICT (player_id) DO NOTHING",
                    new_id(), txn.player_id,
                )
                before = await c.fetchrow("SELECT lifetime_deposited FROM wallets WHERE player_id = $1 FOR UPDATE", txn.player_id)
                is_first_deposit = int(before["lifetime_deposited"]) == 0
                # won_balance is deliberately untouched: deposits are playable, never withdrawable.
                wallet_row = await c.fetchrow(
                    "UPDATE wallets SET balance = balance + $2, lifetime_deposited = lifetime_deposited + $2, "
                    "updated_at = $3 WHERE player_id = $1 RETURNING balance",
                    txn.player_id, txn.amount, utcnow(),
                )
                new_balance = int(wallet_row["balance"])
                resolved_at = utcnow()
                await c.execute(
                    "UPDATE transactions SET type = 'top_up_approved', status = 'completed', "
                    "balance_after = $2, admin_id = $3, resolved_at = $4 WHERE id = $1",
                    transaction_id, new_balance, admin_id, resolved_at,
                )
                txn.type, txn.status = "top_up_approved", "completed"
                txn.balance_after, txn.admin_id, txn.resolved_at = new_balance, admin_id, resolved_at

                extra = None
                if is_first_deposit and on_first_deposit is not None:
                    extra = await on_first_deposit(c, txn)
                approved_extra = None
                if on_approved is not None:
                    approved_extra = await on_approved(c, txn)
                return TopupApproval(txn, True, extra, approved_extra)

    async def apply_delta(self, player_id: str, balance: int = 0, won: int = 0, bonus: int = 0) -> Wallet:
        """Atomically add signed deltas to the wallet pools and return the new wallet.

        The arithmetic happens inside the UPDATE (`col = col + $n`), so concurrent
        credits and debits never overwrite each other the way a read-modify-write
        from Python does. Creates the wallet row first if the player has none.
        """
        await self._execute(
            "INSERT INTO wallets (id, player_id) VALUES ($1, $2) ON CONFLICT (player_id) DO NOTHING",
            new_id(), player_id,
        )
        row = await self._fetchrow(
            "UPDATE wallets SET balance = balance + $2, won_balance = won_balance + $3, "
            "bonus_balance = bonus_balance + $4, updated_at = $5 WHERE player_id = $1 RETURNING *",
            player_id, balance, won, bonus, utcnow(),
        )
        return Wallet(**row)

    async def debit_stake_atomic(
        self, player_id: str, amount: int, funding_mode: str
    ) -> Optional[tuple[Wallet, StakeSplit]]:
        """Charge a stake that may draw on bonus and/or real balance, under a row lock.

        Returns (new wallet, how the stake was split), or None if the wallet cannot
        cover it. The split is decided by `plan_stake_debit` on the locked row, so
        two simultaneous purchases can never both spend the same money.
        """
        pool = await self._pool()
        async with pool.acquire() as c:
            async with c.transaction():
                await c.execute(
                    "INSERT INTO wallets (id, player_id) VALUES ($1, $2) ON CONFLICT (player_id) DO NOTHING",
                    new_id(), player_id,
                )
                row = await c.fetchrow("SELECT * FROM wallets WHERE player_id = $1 FOR UPDATE", player_id)
                split = plan_stake_debit(
                    int(row["balance"]), int(row["won_balance"]), int(row["bonus_balance"]), amount, funding_mode
                )
                if split is None:
                    return None
                updated = await c.fetchrow(
                    "UPDATE wallets SET balance = balance - $2, bonus_balance = bonus_balance - $3, "
                    "won_balance = won_balance - $4, updated_at = $5 WHERE player_id = $1 RETURNING *",
                    player_id, split.from_real, split.from_bonus, split.from_won, utcnow(),
                )
                return Wallet(**dict(updated)), split

    async def hold_withdrawal_atomic(self, txn: Transaction) -> Transaction:
        """Hold a withdrawal: re-check eligibility on the LOCKED wallet row, debit it,
        and insert the pending transaction, all in ONE database transaction.

        The old flow read the wallet, checked `won_balance >= amount`, then wrote
        absolute values, so two simultaneous requests both passed the check and both
        debited from the same stale snapshot (double-spend of winnings). Here the
        check and the debit see the same locked row.

        `txn.amount` is negative (a debit). Raises `WithdrawalRejected`.
        Returns the stored transaction with `balance_after` filled in.
        """
        hold = -txn.amount
        pool = await self._pool()
        async with pool.acquire() as c:
            async with c.transaction():
                row = await c.fetchrow(
                    "SELECT balance, won_balance, lifetime_deposited FROM wallets "
                    "WHERE player_id = $1 FOR UPDATE",
                    txn.player_id,
                )
                if row is None or int(row["lifetime_deposited"]) <= 0:
                    raise WithdrawalRejected("no_deposit")
                if int(row["won_balance"]) < hold or int(row["balance"]) < hold:
                    raise WithdrawalRejected("insufficient_won")
                updated = await c.fetchrow(
                    "UPDATE wallets SET balance = balance - $2, won_balance = won_balance - $2, "
                    "updated_at = $3 WHERE player_id = $1 RETURNING balance",
                    txn.player_id, hold, utcnow(),
                )
                txn.balance_after = int(updated["balance"])
                data = txn.model_dump()
                placeholders = ", ".join(f"${i + 1}" for i in range(len(_TXN_COLUMNS)))
                await c.execute(
                    f"INSERT INTO transactions ({', '.join(_TXN_COLUMNS)}) VALUES ({placeholders})",
                    *(data[col] for col in _TXN_COLUMNS),
                )
                return txn

    async def grant_free_credit_atomic(self, player_id: str, amount: int) -> Optional[Wallet]:
        """Grant the one-time welcome credit exactly once.

        The `free_credit_granted = FALSE` guard lives in the UPDATE's WHERE clause, so
        of N concurrent callers exactly one matches the row; the rest get None.
        """
        await self._execute(
            "INSERT INTO wallets (id, player_id) VALUES ($1, $2) ON CONFLICT (player_id) DO NOTHING",
            new_id(), player_id,
        )
        row = await self._fetchrow(
            "UPDATE wallets SET bonus_balance = bonus_balance + $2, free_credit_granted = TRUE, "
            "updated_at = $3 WHERE player_id = $1 AND free_credit_granted = FALSE RETURNING *",
            player_id, amount, utcnow(),
        )
        return Wallet(**row) if row else None

    async def resolve_withdrawal_atomic(
        self, transaction_id: str, admin_id: str, action: str, note: str = ""
    ) -> tuple[Optional[Transaction], bool]:
        """Approve ("approve") or reject+refund ("reject") a pending withdrawal in ONE
        database transaction, under the transaction row lock.

        Previously the status check, the refund and the status update were separate steps, so
        two admins rejecting at once (or an approve racing a reject) could refund twice or both
        pay and refund. Returns (txn, applied): `applied` is True only for the single call that
        actually resolved it; (None, False) if no such transaction.
        An approve for a legacy request whose player never made a real deposit is converted
        into a reject, as before.
        """
        pool = await self._pool()
        async with pool.acquire() as c:
            async with c.transaction():
                row = await c.fetchrow("SELECT * FROM transactions WHERE id = $1 FOR UPDATE", transaction_id)
                if row is None:
                    return None, False
                txn = Transaction(**dict(row))
                if txn.type != "withdrawal_pending" or txn.status != "pending":
                    return txn, False

                if action == "approve":
                    dep = await c.fetchval(
                        "SELECT lifetime_deposited FROM wallets WHERE player_id = $1", txn.player_id
                    )
                    if dep is None or int(dep) <= 0:
                        action, note = "reject", "Rejected automatically: no approved real-money deposit"

                resolved_at = utcnow()
                if action == "approve":
                    await c.execute(
                        "UPDATE transactions SET type = 'withdrawal_approved', status = 'completed', "
                        "admin_id = $2, resolved_at = $3 WHERE id = $1",
                        transaction_id, admin_id, resolved_at,
                    )
                    txn.type, txn.status = "withdrawal_approved", "completed"
                else:
                    refund = abs(txn.amount)
                    await c.execute(
                        "INSERT INTO wallets (id, player_id) VALUES ($1, $2) ON CONFLICT (player_id) DO NOTHING",
                        new_id(), txn.player_id,
                    )
                    await c.execute(
                        "UPDATE wallets SET balance = balance + $2, won_balance = won_balance + $2, "
                        "updated_at = $3 WHERE player_id = $1",
                        txn.player_id, refund, resolved_at,
                    )
                    await c.execute(
                        "UPDATE transactions SET type = 'withdrawal_rejected', status = 'rejected', "
                        "admin_id = $2, note = $3, resolved_at = $4 WHERE id = $1",
                        transaction_id, admin_id, note, resolved_at,
                    )
                    txn.type, txn.status, txn.note = "withdrawal_rejected", "rejected", note
                txn.admin_id, txn.resolved_at = admin_id, resolved_at
                return txn, True
