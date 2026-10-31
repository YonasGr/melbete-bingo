"""Postgres repository for player engagement state: play streaks and nudge / win-back cooldowns.

Every "may I send / pay this?" decision is a single guarded UPDATE ... RETURNING (claim_*), so two
workers or two overlapping job runs can never both win the same claim.
"""
from datetime import date, datetime
from typing import Optional

from src.repositories.base import BaseRepository

# Players we may message: a real Telegram user, not banned/bot, and not opted out of promotions.
_PROMO_OK = (
    "p.is_banned = FALSE AND p.is_bot = FALSE AND p.promo_opt_out = FALSE AND p.telegram_id ~ '^[0-9]+$'"
)


class EngagementRepository(BaseRepository):
    table_name = "engagement_state"

    async def record_play(self, player_id: str, play_day: date, now: datetime) -> dict:
        """Register that the player played a round on `play_day` (EAT date). Updates the streak:
        same day = unchanged, the next day = +1, a gap = back to 1 (and rewards start over).
        Locks the player's row, so concurrent calls cannot double-count a day."""
        pool = await self._pool()
        async with pool.acquire() as c:
            async with c.transaction():
                await c.execute("INSERT INTO engagement_state (player_id) VALUES ($1) ON CONFLICT DO NOTHING", player_id)
                row = await c.fetchrow("SELECT * FROM engagement_state WHERE player_id = $1 FOR UPDATE", player_id)
                last = row["last_play_day"]
                streak, rewarded = int(row["streak_days"]), int(row["last_rewarded_streak"])
                if last == play_day:
                    pass
                elif last is not None and (play_day - last).days == 1:
                    streak += 1
                else:
                    streak, rewarded = 1, 0
                best = max(int(row["best_streak"]), streak)
                updated = await c.fetchrow(
                    "UPDATE engagement_state SET last_played_at = $2, last_play_day = $3, streak_days = $4, "
                    "best_streak = $5, last_rewarded_streak = $6, updated_at = $2 WHERE player_id = $1 RETURNING *",
                    player_id, now, play_day, streak, best, rewarded,
                )
                return dict(updated)

    async def claim_streak_reward(self, player_id: str, milestone: int) -> bool:
        """True for exactly one caller per milestone per streak run."""
        row = await self._fetchrow(
            "UPDATE engagement_state SET last_rewarded_streak = $2 WHERE player_id = $1 "
            "AND last_rewarded_streak < $2 RETURNING player_id", player_id, milestone,
        )
        return row is not None

    async def lapsed_candidates(self, lapsed_before: datetime, not_older_than: datetime,
                                cooldown_cutoff: datetime, limit: int) -> list[dict]:
        return await self._fetch(
            f"SELECT e.player_id, p.telegram_id FROM engagement_state e JOIN players p ON p.id = e.player_id "
            f"WHERE e.last_played_at < $1 AND e.last_played_at >= $2 "
            f"AND (e.last_winback_at IS NULL OR e.last_winback_at < $3) AND {_PROMO_OK} "
            f"ORDER BY e.last_played_at DESC LIMIT $4",
            lapsed_before, not_older_than, cooldown_cutoff, limit,
        )

    async def claim_winback(self, player_id: str, cooldown_cutoff: datetime, now: datetime) -> bool:
        row = await self._fetchrow(
            "UPDATE engagement_state SET last_winback_at = $3 WHERE player_id = $1 "
            "AND (last_winback_at IS NULL OR last_winback_at < $2) RETURNING player_id",
            player_id, cooldown_cutoff, now,
        )
        return row is not None

    async def release_winback(self, player_id: str, previous: Optional[datetime]) -> None:
        """Undo a claim_winback when the bonus could not be paid, so the player is not skipped."""
        await self._execute("UPDATE engagement_state SET last_winback_at = $2 WHERE player_id = $1", player_id, previous)

    async def winback_paid_since(self, since: datetime) -> int:
        return int(await self._fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'winback_bonus' AND created_at >= $1", since
        ) or 0)

    async def nudge_candidates(self, active_since: datetime, cooldown_cutoff: datetime,
                               exclude_player_ids: list[str], limit: int) -> list[dict]:
        return await self._fetch(
            f"SELECT e.player_id, p.telegram_id FROM engagement_state e JOIN players p ON p.id = e.player_id "
            f"WHERE e.last_played_at >= $1 AND (e.last_nudge_at IS NULL OR e.last_nudge_at < $2) "
            f"AND NOT (e.player_id = ANY($3::text[])) AND {_PROMO_OK} ORDER BY e.last_played_at DESC LIMIT $4",
            active_since, cooldown_cutoff, exclude_player_ids, limit,
        )

    async def claim_nudge(self, player_id: str, cooldown_cutoff: datetime, now: datetime) -> bool:
        row = await self._fetchrow(
            "UPDATE engagement_state SET last_nudge_at = $3 WHERE player_id = $1 "
            "AND (last_nudge_at IS NULL OR last_nudge_at < $2) RETURNING player_id",
            player_id, cooldown_cutoff, now,
        )
        return row is not None

    async def streak_reminder_candidates(self, today: date, limit: int) -> list[dict]:
        """Players on a streak who played YESTERDAY but not yet today (so today is the day to keep it)."""
        return await self._fetch(
            f"SELECT e.player_id, e.streak_days, p.telegram_id FROM engagement_state e "
            f"JOIN players p ON p.id = e.player_id WHERE e.last_play_day = $1::date - 1 AND e.streak_days >= 1 "
            f"AND (e.last_streak_reminder_day IS NULL OR e.last_streak_reminder_day < $1) AND {_PROMO_OK} LIMIT $2",
            today, limit,
        )

    async def claim_streak_reminder(self, player_id: str, today: date) -> bool:
        row = await self._fetchrow(
            "UPDATE engagement_state SET last_streak_reminder_day = $2 WHERE player_id = $1 "
            "AND (last_streak_reminder_day IS NULL OR last_streak_reminder_day < $2) RETURNING player_id",
            player_id, today,
        )
        return row is not None
