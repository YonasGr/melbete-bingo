"""Postgres repository for leaderboard campaigns, points, channel attempts and approved rewards."""
import json
from datetime import datetime
from typing import Optional

from src.models.schemas import new_id, utcnow
from src.repositories.base import BaseRepository

_CAMPAIGN_FIELDS = (
    "name", "timezone", "starts_at", "ends_at", "budget_etb", "winners_count", "points_per_cartela",
    "wagering_multiplier", "channel_enabled", "channel_username", "channel_points", "cancelled", "reward_tiers",
)


def _norm(row: Optional[dict]) -> Optional[dict]:
    """jsonb comes back from asyncpg as text; hand the service a real list."""
    if row is not None and isinstance(row.get("reward_tiers"), str):
        row["reward_tiers"] = json.loads(row["reward_tiers"])
    return row


def _db_value(key: str, value):
    return json.dumps(value) if key == "reward_tiers" and value is not None else value

# One row per player with their totals; banned accounts and bots never appear on a leaderboard.
_STANDINGS_SQL = """
    SELECT lp.player_id,
           SUM(lp.points)::int AS points,
           COALESCE(SUM(lp.points) FILTER (WHERE lp.source = 'cartela'), 0)::int AS cartela_points,
           COALESCE(SUM(lp.points) FILTER (WHERE lp.source = 'channel'), 0)::int AS channel_points,
           COALESCE(SUM(lp.stake_etb) FILTER (WHERE lp.source = 'cartela'), 0)::int AS reserved_etb,
           COUNT(*) FILTER (WHERE lp.source = 'cartela')::int AS cartelas,
           MAX(lp.created_at) AS last_at,
           pl.display_name
    FROM leaderboard_points lp
    JOIN players pl ON pl.id = lp.player_id
    WHERE lp.campaign_id = $1 AND NOT lp.revoked AND NOT pl.is_banned AND NOT pl.is_bot
    GROUP BY lp.player_id, pl.display_name
    HAVING SUM(lp.points) > 0
"""
# Ranking: most points; a tie goes to the player who reserved more ETB, then to whoever got there first.
_RANK_ORDER = "points DESC, reserved_etb DESC, last_at ASC"


class LeaderboardRepository(BaseRepository):
    table_name = "leaderboard_campaigns"

    # ------------------------------------------------------------------ campaigns
    async def create_campaign(self, values: dict, created_by: str) -> dict:
        cols = ("id",) + _CAMPAIGN_FIELDS + ("created_by",)
        args = [new_id()] + [_db_value(k, values.get(k)) for k in _CAMPAIGN_FIELDS] + [created_by]
        ph = ", ".join(f"${i + 1}" for i in range(len(cols)))
        return _norm(await self._fetchrow(
            f"INSERT INTO leaderboard_campaigns ({', '.join(cols)}) VALUES ({ph}) RETURNING *", *args
        ))

    async def update_campaign(self, campaign_id: str, fields: dict) -> Optional[dict]:
        keys = [k for k in fields if k in _CAMPAIGN_FIELDS]
        if not keys:
            return await self.get_campaign(campaign_id)
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(keys))
        return _norm(await self._fetchrow(
            f"UPDATE leaderboard_campaigns SET {sets}, updated_at = ${len(keys) + 2} WHERE id = $1 RETURNING *",
            campaign_id, *[_db_value(k, fields[k]) for k in keys], utcnow(),
        ))

    async def get_campaign(self, campaign_id: str) -> Optional[dict]:
        return _norm(await self._fetchrow("SELECT * FROM leaderboard_campaigns WHERE id = $1", campaign_id))

    async def list_campaigns(self, limit: int = 50) -> list[dict]:
        return [_norm(r) for r in await self._fetch("SELECT * FROM leaderboard_campaigns ORDER BY starts_at DESC LIMIT $1", limit)]

    async def overlapping(self, starts_at: datetime, ends_at: datetime, exclude_id: Optional[str] = None) -> Optional[dict]:
        return _norm(await self._fetchrow(
            "SELECT * FROM leaderboard_campaigns WHERE NOT cancelled AND starts_at < $2 AND ends_at > $1 "
            "AND ($3::text IS NULL OR id <> $3) LIMIT 1",
            starts_at, ends_at, exclude_id,
        ))

    async def get_active(self, now: datetime) -> Optional[dict]:
        return _norm(await self._fetchrow(
            "SELECT * FROM leaderboard_campaigns WHERE NOT cancelled AND starts_at <= $1 AND ends_at > $1 LIMIT 1", now,
        ))

    async def get_upcoming(self, now: datetime) -> Optional[dict]:
        return _norm(await self._fetchrow(
            "SELECT * FROM leaderboard_campaigns WHERE NOT cancelled AND starts_at > $1 ORDER BY starts_at LIMIT 1", now,
        ))

    async def get_latest_ended(self, now: datetime) -> Optional[dict]:
        return _norm(await self._fetchrow(
            "SELECT * FROM leaderboard_campaigns WHERE NOT cancelled AND ends_at <= $1 ORDER BY ends_at DESC LIMIT 1", now,
        ))

    async def decide(self, campaign_id: str, status: str, admin_id: str, note: str, now: datetime) -> Optional[dict]:
        """Claim the one-and-only distribution decision. None if already decided or the campaign has not ended."""
        return _norm(await self._fetchrow(
            "UPDATE leaderboard_campaigns SET distribution_status = $2, decided_by = $3, decision_note = $4, "
            "decided_at = $5, updated_at = $5 WHERE id = $1 AND distribution_status = 'none' AND NOT cancelled "
            "AND ends_at <= $5 RETURNING *",
            campaign_id, status, admin_id, note, now,
        ))

    async def set_total_distributed(self, campaign_id: str, total: int) -> None:
        await self._execute("UPDATE leaderboard_campaigns SET total_distributed = $2 WHERE id = $1", campaign_id, total)

    # ------------------------------------------------------------------ points
    async def add_points(self, campaign_id: str, player_id: str, source: str, points: int, dedupe_key: str, note: str = "",
                         stake_etb: int = 0) -> bool:
        row = await self._fetchrow(
            "INSERT INTO leaderboard_points (id, campaign_id, player_id, source, points, dedupe_key, note, stake_etb) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING RETURNING id",
            new_id(), campaign_id, player_id, source, points, dedupe_key, note, max(0, int(stake_etb)),
        )
        return row is not None

    async def standings(self, campaign_id: str, limit: int = 100) -> list[dict]:
        return await self._fetch(_STANDINGS_SQL + f" ORDER BY {_RANK_ORDER} LIMIT $2", campaign_id, limit)

    async def standing_at_rank(self, campaign_id: str, rank: int) -> Optional[dict]:
        return await self._fetchrow(_STANDINGS_SQL + f" ORDER BY {_RANK_ORDER} OFFSET $2 LIMIT 1", campaign_id, max(0, rank - 1))

    async def player_standing(self, campaign_id: str, player_id: str) -> Optional[dict]:
        return await self._fetchrow(
            "SELECT * FROM (SELECT t.*, ROW_NUMBER() OVER (ORDER BY points DESC, reserved_etb DESC, last_at ASC) AS rank FROM ("
            + _STANDINGS_SQL + ") t) r WHERE r.player_id = $2",
            campaign_id, player_id,
        )

    async def participant_count(self, campaign_id: str) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM (" + _STANDINGS_SQL + ") t", campaign_id) or 0)

    async def player_history(self, campaign_id: str, player_id: str, limit: int = 30) -> list[dict]:
        """Channel reward entries (one row each) plus cartela points grouped per day, newest first."""
        return await self._fetch(
            "SELECT source, SUM(points)::int AS points, COUNT(*)::int AS entries, MAX(created_at) AS last_at, "
            "BOOL_OR(revoked) AS revoked FROM leaderboard_points WHERE campaign_id = $1 AND player_id = $2 "
            "GROUP BY source, CASE WHEN source = 'channel' THEN id ELSE date_trunc('day', created_at)::text END "
            "ORDER BY last_at DESC LIMIT $3",
            campaign_id, player_id, limit,
        )

    async def channel_claims(self, campaign_id: str, limit: int = 200) -> list[dict]:
        return await self._fetch(
            "SELECT lp.id, lp.player_id, lp.points, lp.note, lp.revoked, lp.revoked_by, lp.revoked_at, lp.created_at, "
            "pl.display_name, pl.telegram_id FROM leaderboard_points lp LEFT JOIN players pl ON pl.id = lp.player_id "
            "WHERE lp.campaign_id = $1 AND lp.source = 'channel' ORDER BY lp.created_at DESC LIMIT $2",
            campaign_id, limit,
        )

    async def revoke_points(self, points_id: str, admin_id: str) -> Optional[dict]:
        return await self._fetchrow(
            "UPDATE leaderboard_points SET revoked = TRUE, revoked_by = $2, revoked_at = $3 "
            "WHERE id = $1 AND NOT revoked RETURNING *",
            points_id, admin_id, utcnow(),
        )

    # ------------------------------------------------------------------ channel attempts
    async def record_attempt(self, campaign_id: str, player_id: str, result: str, detail: str = "") -> None:
        await self._execute(
            "INSERT INTO leaderboard_channel_attempts (id, campaign_id, player_id, result, detail) VALUES ($1, $2, $3, $4, $5)",
            new_id(), campaign_id, player_id, result, detail[:300],
        )

    async def recent_attempts(self, campaign_id: str, player_id: str, since: datetime) -> int:
        return int(await self._fetchval(
            "SELECT COUNT(*) FROM leaderboard_channel_attempts WHERE campaign_id = $1 AND player_id = $2 AND created_at >= $3",
            campaign_id, player_id, since,
        ) or 0)

    async def list_attempts(self, campaign_id: str, limit: int = 100) -> list[dict]:
        return await self._fetch(
            "SELECT a.*, pl.display_name, pl.telegram_id FROM leaderboard_channel_attempts a "
            "LEFT JOIN players pl ON pl.id = a.player_id WHERE a.campaign_id = $1 AND a.result <> 'credited' "
            "ORDER BY a.created_at DESC LIMIT $2",
            campaign_id, limit,
        )

    # ------------------------------------------------------------------ rewards and wagering
    async def insert_reward(self, campaign_id: str, player_id: str, rank: int, points: int, amount: int, wager_required: int) -> Optional[dict]:
        return await self._fetchrow(
            "INSERT INTO leaderboard_rewards (id, campaign_id, player_id, rank, points, amount_etb, wager_required) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT DO NOTHING RETURNING *",
            new_id(), campaign_id, player_id, rank, points, amount, wager_required,
        )

    async def uncredited_rewards(self, campaign_id: str) -> list[dict]:
        return await self._fetch(
            "SELECT * FROM leaderboard_rewards WHERE campaign_id = $1 AND credited_at IS NULL ORDER BY rank", campaign_id,
        )

    async def mark_credited(self, reward_id: str) -> bool:
        row = await self._fetchrow(
            "UPDATE leaderboard_rewards SET credited_at = $2 WHERE id = $1 AND credited_at IS NULL RETURNING id",
            reward_id, utcnow(),
        )
        return row is not None

    async def list_rewards(self, campaign_id: str, limit: int = 1000) -> list[dict]:
        return await self._fetch(
            "SELECT r.*, pl.display_name, pl.telegram_id FROM leaderboard_rewards r LEFT JOIN players pl ON pl.id = r.player_id "
            "WHERE r.campaign_id = $1 ORDER BY r.rank LIMIT $2",
            campaign_id, limit,
        )

    async def player_rewards(self, player_id: str, limit: int = 10) -> list[dict]:
        return await self._fetch(
            "SELECT r.*, c.name AS campaign_name FROM leaderboard_rewards r JOIN leaderboard_campaigns c ON c.id = r.campaign_id "
            "WHERE r.player_id = $1 AND r.credited_at IS NOT NULL ORDER BY r.created_at DESC LIMIT $2",
            player_id, limit,
        )

    async def add_wagered(self, player_id: str, stake: int) -> None:
        await self._execute(
            "UPDATE leaderboard_rewards SET wagered = wagered + $2 WHERE player_id = $1 AND status = 'locked' AND credited_at IS NOT NULL",
            player_id, stake,
        )

    async def add_bonus_won(self, player_id: str, amount: int) -> None:
        """Winnings that landed in the bonus balance while a leaderboard reward was still locked."""
        if int(amount) <= 0:
            return
        await self._execute(
            "UPDATE leaderboard_rewards SET bonus_won = bonus_won + $2 WHERE player_id = $1 AND status = 'locked' AND credited_at IS NOT NULL",
            player_id, int(amount),
        )

    async def due_release(self, player_id: str) -> list[dict]:
        return await self._fetch(
            "SELECT * FROM leaderboard_rewards WHERE player_id = $1 AND status = 'locked' AND credited_at IS NOT NULL "
            "AND wagered >= wager_required ORDER BY created_at",
            player_id,
        )

    async def claim_release(self, reward_id: str) -> Optional[dict]:
        return await self._fetchrow(
            "UPDATE leaderboard_rewards SET status = 'released', released_at = $2 WHERE id = $1 AND status = 'locked' RETURNING *",
            reward_id, utcnow(),
        )

    async def finish_release(self, reward_id: str, released_etb: int) -> None:
        await self._execute("UPDATE leaderboard_rewards SET released_etb = $2 WHERE id = $1", reward_id, released_etb)

    async def undo_release(self, reward_id: str) -> None:
        await self._execute("UPDATE leaderboard_rewards SET status = 'locked', released_at = NULL WHERE id = $1", reward_id)

    async def awaiting_decision_count(self, now: datetime) -> int:
        return int(await self._fetchval(
            "SELECT COUNT(*) FROM leaderboard_campaigns WHERE NOT cancelled AND ends_at <= $1 AND distribution_status = 'none'", now,
        ) or 0)
