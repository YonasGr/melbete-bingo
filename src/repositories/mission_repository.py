"""Postgres repository for daily missions."""
from datetime import date, datetime

from src.repositories.base import BaseRepository


class MissionRepository(BaseRepository):
    table_name = "daily_mission_claims"

    async def rounds_played(self, player_id: str, since: datetime) -> int:
        """Distinct rounds the player actually paid into since `since` (reserved-but-dropped cartelas do not count)."""
        return int(await self._fetchval(
            "SELECT COUNT(DISTINCT c.room_id) FROM cards c JOIN game_rooms r ON r.id = c.room_id "
            "WHERE c.owner_id = $1 AND c.created_at >= $2 AND c.funded_by <> 'unfunded' AND r.state IN ('running', 'ended')",
            player_id, since,
        ) or 0)

    async def claimed(self, player_id: str, day: date) -> set[int]:
        rows = await self._fetch("SELECT rounds FROM daily_mission_claims WHERE player_id = $1 AND day = $2", player_id, day)
        return {int(r["rounds"]) for r in rows}

    async def paid_on(self, day: date) -> int:
        return int(await self._fetchval("SELECT COALESCE(SUM(amount), 0) FROM daily_mission_claims WHERE day = $1", day) or 0)

    async def claim(self, player_id: str, day: date, rounds: int, amount: int) -> bool:
        row = await self._fetchrow(
            "INSERT INTO daily_mission_claims (player_id, day, rounds, amount) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT DO NOTHING RETURNING rounds",
            player_id, day, rounds, amount,
        )
        return row is not None

    async def unclaim(self, player_id: str, day: date, rounds: int) -> None:
        await self._execute("DELETE FROM daily_mission_claims WHERE player_id = $1 AND day = $2 AND rounds = $3", player_id, day, rounds)
