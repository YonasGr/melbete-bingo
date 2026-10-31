"""Read-only queries behind the live admin dashboard."""
from datetime import datetime
from typing import List

from src.repositories.base import BaseRepository


class LiveStatsRepository(BaseRepository):
    table_name = "transactions"

    async def window_stats(self, start: datetime, end: datetime) -> dict:
        """Money and activity between two instants. Stakes are stored negative, withdrawals negative."""
        row = await self._fetchrow(
            """
            SELECT
              COALESCE(SUM(-amount) FILTER (WHERE type = 'game_stake'   AND created_at >= $1 AND created_at < $2), 0) AS stakes,
              COALESCE(SUM(amount)  FILTER (WHERE type = 'win_payout'   AND created_at >= $1 AND created_at < $2), 0) AS payouts,
              COALESCE(SUM(amount)  FILTER (WHERE type = 'stake_refund' AND created_at >= $1 AND created_at < $2), 0) AS refunds,
              COALESCE(SUM(amount)  FILTER (WHERE type = 'top_up_approved' AND COALESCE(resolved_at, created_at) >= $1 AND COALESCE(resolved_at, created_at) < $2), 0) AS deposits,
              COUNT(*)              FILTER (WHERE type = 'top_up_approved' AND COALESCE(resolved_at, created_at) >= $1 AND COALESCE(resolved_at, created_at) < $2) AS deposit_count,
              COALESCE(SUM(-amount) FILTER (WHERE type = 'withdrawal_approved' AND COALESCE(resolved_at, created_at) >= $1 AND COALESCE(resolved_at, created_at) < $2), 0) AS withdrawals,
              COUNT(*)              FILTER (WHERE type = 'withdrawal_approved' AND COALESCE(resolved_at, created_at) >= $1 AND COALESCE(resolved_at, created_at) < $2) AS withdrawal_count,
              COUNT(DISTINCT player_id) FILTER (WHERE type = 'game_stake' AND created_at >= $1 AND created_at < $2) AS players_played,
              COUNT(*)              FILTER (WHERE type = 'game_stake' AND created_at >= $1 AND created_at < $2) AS stake_count
            FROM transactions
            WHERE created_at >= $1 - interval '14 days'
            """,
            start, end,
        )
        return {k: int(v or 0) for k, v in dict(row).items()} if row else {}

    async def pending(self) -> dict:
        rows = await self._fetch(
            "SELECT type, COUNT(*) AS n, COALESCE(SUM(ABS(amount)), 0) AS amount, MIN(created_at) AS oldest "
            "FROM transactions WHERE status = 'pending' AND type IN ('top_up_pending', 'withdrawal_pending') GROUP BY type"
        )
        return {r["type"]: {"count": int(r["n"]), "amount": int(r["amount"]), "oldest": r["oldest"]} for r in rows}

    async def new_players(self, since: datetime) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM players WHERE created_at >= $1 AND NOT is_bot", since) or 0)

    async def rounds_since(self, since: datetime) -> dict:
        row = await self._fetchrow(
            "SELECT COUNT(*) AS started, COUNT(*) FILTER (WHERE state = 'ended') AS ended "
            "FROM game_rooms WHERE created_at >= $1",
            since,
        )
        return {"started": int(row["started"] or 0), "ended": int(row["ended"] or 0)} if row else {"started": 0, "ended": 0}

    async def daily_rows(self, since: datetime, offset_hours: int) -> tuple[List[dict], List[dict]]:
        """Per local day: stakes/payouts/refunds, and approved deposits."""
        stakes = await self._fetch(
            """
            SELECT (created_at + make_interval(hours => $2))::date AS day,
              COALESCE(SUM(-amount) FILTER (WHERE type = 'game_stake'), 0)   AS stakes,
              COALESCE(SUM(amount)  FILTER (WHERE type = 'win_payout'), 0)   AS payouts,
              COALESCE(SUM(amount)  FILTER (WHERE type = 'stake_refund'), 0) AS refunds
            FROM transactions
            WHERE created_at >= $1 AND type IN ('game_stake', 'win_payout', 'stake_refund')
            GROUP BY 1
            """,
            since, offset_hours,
        )
        deposits = await self._fetch(
            """
            SELECT (COALESCE(resolved_at, created_at) + make_interval(hours => $2))::date AS day,
              COALESCE(SUM(amount), 0) AS deposits
            FROM transactions
            WHERE type = 'top_up_approved' AND COALESCE(resolved_at, created_at) >= $1
            GROUP BY 1
            """,
            since, offset_hours,
        )
        return [dict(r) for r in stakes], [dict(r) for r in deposits]
