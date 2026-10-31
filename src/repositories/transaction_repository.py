"""Postgres repository for wallet transactions (the audit ledger)."""
from typing import List, Optional

from src.models.schemas import Transaction
from src.repositories.base import BaseRepository

_COLUMNS = (
    "id", "player_id", "type", "amount", "balance_after", "status",
    "payment_method", "screenshot_file_id", "screenshot_url", "room_id",
    "admin_id", "note", "sms_text", "payment_verification_url", "legal_name", "withdrawal_address",
    "notification_chat_id", "notification_message_id", "notification_is_photo",
    "created_at", "resolved_at",
)


class TransactionRepository(BaseRepository):
    table_name = "transactions"

    async def create(self, txn: Transaction) -> Transaction:
        data = txn.model_dump()
        placeholders = ", ".join(f"${i + 1}" for i in range(len(_COLUMNS)))
        columns = ", ".join(_COLUMNS)
        await self._execute(
            f"INSERT INTO transactions ({columns}) VALUES ({placeholders})",
            *(data[c] for c in _COLUMNS),
        )
        return txn

    async def get(self, transaction_id: str) -> Optional[Transaction]:
        row = await self._fetchrow("SELECT * FROM transactions WHERE id = $1", transaction_id)
        return Transaction(**row) if row else None

    async def get_by_payment_url(self, url: str) -> Optional[Transaction]:
        if not url:
            return None
        row = await self._fetchrow("SELECT * FROM transactions WHERE payment_verification_url = $1", url)
        return Transaction(**row) if row else None

    async def update(self, transaction_id: str, fields: dict) -> None:
        await self._update_fields("transactions", "id", transaction_id, fields)

    async def set_notification_ref(
        self, transaction_id: str, chat_id: str, message_id: int, is_photo: bool
    ) -> None:
        await self.update(
            transaction_id,
            {
                "notification_chat_id": chat_id,
                "notification_message_id": message_id,
                "notification_is_photo": is_photo,
            },
        )

    async def list_pending_topups(self) -> List[Transaction]:
        rows = await self._fetch(
            "SELECT * FROM transactions WHERE type = 'top_up_pending' AND status = 'pending' "
            "ORDER BY created_at ASC"
        )
        return [Transaction(**r) for r in rows]

    async def list_pending_withdrawals(self) -> List[Transaction]:
        rows = await self._fetch(
            "SELECT * FROM transactions WHERE type = 'withdrawal_pending' AND status = 'pending' "
            "ORDER BY created_at ASC"
        )
        return [Transaction(**r) for r in rows]

    async def list_deposit_history(self, limit: int = 200, status_filter: Optional[str] = None) -> List[Transaction]:
        if status_filter and status_filter != "all":
            if status_filter == "approved":
                target_types = ('top_up_approved',)
            elif status_filter == "rejected":
                target_types = ('top_up_rejected',)
            elif status_filter == "pending":
                target_types = ('top_up_pending',)
            else:
                target_types = ('top_up_approved', 'top_up_rejected', 'top_up_pending')
            
            rows = await self._fetch(
                "SELECT * FROM transactions WHERE type = ANY($1) ORDER BY created_at DESC LIMIT $2",
                list(target_types), limit,
            )
        else:
            rows = await self._fetch(
                "SELECT * FROM transactions WHERE type LIKE 'top_up_%' ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [Transaction(**r) for r in rows]

    async def list_withdrawal_history(self, limit: int = 200, status_filter: Optional[str] = None) -> List[Transaction]:
        if status_filter and status_filter != "all":
            if status_filter == "approved":
                target_types = ('withdrawal_approved',)
            elif status_filter == "rejected":
                target_types = ('withdrawal_rejected',)
            elif status_filter == "pending":
                target_types = ('withdrawal_pending',)
            else:
                target_types = ('withdrawal_approved', 'withdrawal_rejected', 'withdrawal_pending')

            rows = await self._fetch(
                "SELECT * FROM transactions WHERE type = ANY($1) ORDER BY created_at DESC LIMIT $2",
                list(target_types), limit,
            )
        else:
            rows = await self._fetch(
                "SELECT * FROM transactions WHERE type LIKE 'withdrawal_%' ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [Transaction(**r) for r in rows]

    async def list_by_player(self, player_id: str, limit: int = 50) -> List[Transaction]:
        rows = await self._fetch(
            "SELECT * FROM transactions WHERE player_id = $1 ORDER BY created_at DESC LIMIT $2",
            player_id, limit,
        )
        return [Transaction(**r) for r in rows]

    async def get_economy_summary(self) -> dict:
        """Aggregate financial summary across all transactions for the Economy tab."""
        row = await self._fetchrow("""
            SELECT
                COALESCE(SUM(CASE WHEN type = 'top_up_approved' THEN amount ELSE 0 END), 0) AS total_deposited,
                COALESCE(SUM(CASE WHEN type = 'withdrawal_approved' THEN amount ELSE 0 END), 0) AS total_withdrawn,
                COALESCE(SUM(CASE WHEN type = 'game_stake' THEN amount ELSE 0 END), 0) AS total_stakes_collected,
                COALESCE(SUM(CASE WHEN type = 'win_payout' THEN amount ELSE 0 END), 0) AS total_payouts,
                COALESCE(SUM(CASE WHEN type = 'stake_refund' THEN amount ELSE 0 END), 0) AS total_refunds,
                COALESCE(SUM(CASE WHEN type = 'free_credit' THEN amount ELSE 0 END), 0) AS total_free_credit_issued,
                COALESCE(SUM(CASE WHEN type = 'referral_bonus' THEN amount ELSE 0 END), 0) AS total_referral_bonuses,
                COALESCE(SUM(CASE WHEN type = 'admin_adjustment' THEN amount ELSE 0 END), 0) AS total_admin_adjustments,
                COUNT(DISTINCT player_id) FILTER (WHERE type IN ('game_stake', 'top_up_approved', 'withdrawal_approved')) AS active_wallets,
                COUNT(*) FILTER (WHERE type = 'top_up_approved') AS deposit_count,
                COUNT(*) FILTER (WHERE type = 'withdrawal_approved') AS withdrawal_count,
                COUNT(*) FILTER (WHERE type = 'game_stake') AS stake_count,
                COUNT(*) FILTER (WHERE type = 'win_payout') AS payout_count
            FROM transactions
        """)
        return dict(row) if row else {}

    async def get_economy_time_series(self, days: int = 30) -> list[dict]:
        """Daily aggregated totals for the last N days — used for the revenue chart."""
        rows = await self._fetch("""
            SELECT
                DATE(created_at AT TIME ZONE 'UTC') AS day,
                COALESCE(SUM(CASE WHEN type = 'top_up_approved' THEN amount ELSE 0 END), 0) AS deposited,
                COALESCE(SUM(CASE WHEN type = 'withdrawal_approved' THEN amount ELSE 0 END), 0) AS withdrawn,
                COALESCE(SUM(CASE WHEN type = 'game_stake' THEN amount ELSE 0 END), 0) AS stakes,
                COALESCE(SUM(CASE WHEN type = 'win_payout' THEN amount ELSE 0 END), 0) AS payouts,
                COALESCE(SUM(CASE WHEN type = 'stake_refund' THEN amount ELSE 0 END), 0) AS refunds
            FROM transactions
            WHERE created_at >= NOW() - INTERVAL '1 day' * $1
            GROUP BY day
            ORDER BY day ASC
        """, days)
        return [dict(r) for r in rows]

    async def get_top_depositors(self, limit: int = 10) -> list[dict]:
        """Top players by total approved deposits."""
        rows = await self._fetch("""
            SELECT
                player_id,
                SUM(amount) AS total_deposited,
                COUNT(*) AS deposit_count,
                MAX(created_at) AS last_deposit_at
            FROM transactions
            WHERE type = 'top_up_approved'
            GROUP BY player_id
            ORDER BY total_deposited DESC
            LIMIT $1
        """, limit)
        return [dict(r) for r in rows]

    async def get_top_winners(self, limit: int = 10) -> list[dict]:
        """Top players by total win payouts."""
        rows = await self._fetch("""
            SELECT
                player_id,
                SUM(amount) AS total_won,
                COUNT(*) AS win_count,
                MAX(created_at) AS last_win_at
            FROM transactions
            WHERE type = 'win_payout'
            GROUP BY player_id
            ORDER BY total_won DESC
            LIMIT $1
        """, limit)
        return [dict(r) for r in rows]

    async def list_by_player_full(self, player_id: str) -> list[dict]:
        """Full transaction history for a single player — for the per-user economy drilldown."""
        rows = await self._fetch("""
            SELECT id, type, amount, balance_after, status, payment_method,
                   room_id, admin_id, note, created_at, resolved_at
            FROM transactions
            WHERE player_id = $1
            ORDER BY created_at DESC
        """, player_id)
        return [dict(r) for r in rows]
