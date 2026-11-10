"""Postgres repository for game rooms."""
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from src.models.schemas import GameRoom, utcnow
from src.repositories.base import BaseRepository

_COLUMNS = (
    "id", "track", "host_id", "variant", "number_range_min", "number_range_max",
    "cards_per_player", "pattern", "state", "called_numbers", "draw_pool",
    "winners", "end_reason", "pending_winner", "advanced", "draw_interval",
    "auto_draw", "stake_amount", "prize_pool", "prize_pool_real", "prize_pool_bonus",
    "taken_cartelas", "min_players_to_start", "min_cartelas_to_start", "countdown_seconds", "cartela_min", "cartela_max",
    "countdown_started_at", "created_at", "updated_at",
)


class RoomRepository(BaseRepository):
    table_name = "game_rooms"

    async def get(self, room_id: str) -> Optional[GameRoom]:
        row = await self._fetchrow("SELECT * FROM game_rooms WHERE id = $1", room_id)
        return GameRoom(**row) if row else None

    async def create(self, room: GameRoom) -> GameRoom:
        data = room.model_dump()
        placeholders = ", ".join(f"${i + 1}" for i in range(len(_COLUMNS)))
        columns = ", ".join(_COLUMNS)
        await self._execute(
            f"INSERT INTO game_rooms ({columns}) VALUES ({placeholders})",
            *(data[c] for c in _COLUMNS),
        )
        return room

    async def create_if_no_active(self, room: GameRoom) -> bool:
        """Atomically create a waiting room only when no room is live on
        this track. Each track (real_10, real_50, bonus) gets its own
        advisory lock key, so the 3 tracks can each have a live room at
        the same time - the invariant is "one active room per track",
        not "one active room total".

        PostgreSQL's advisory lock makes this invariant hold across API
        workers and deployments, not merely inside one Python process.
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"room-create:{room.track}")
                active = await conn.fetchval(
                    "SELECT 1 FROM game_rooms WHERE state IN ('waiting', 'running') AND track = $1 LIMIT 1",
                    room.track,
                )
                if active:
                    return False
                placeholders = ", ".join(f"${i + 1}" for i in range(len(_COLUMNS)))
                columns = ", ".join(_COLUMNS)
                await conn.execute(
                    f"INSERT INTO game_rooms ({columns}) VALUES ({placeholders})",
                    *(room.model_dump()[c] for c in _COLUMNS),
                )
        return True

    async def update(self, room_id: str, fields: Dict[str, Any]) -> None:
        fields = {**fields, "updated_at": utcnow()}
        await self._update_fields("game_rooms", "id", room_id, fields)

    async def get_latest(self, track: str) -> Optional[GameRoom]:
        """The most recently created session for this track, if any. Used
        to check whether a live round is already in progress on that
        track before starting a new one, and to decide whether an
        ended-but-not-yet-advanced round should still be shown."""
        row = await self._fetchrow(
            "SELECT * FROM game_rooms WHERE track = $1 ORDER BY created_at DESC LIMIT 1", track
        )
        return GameRoom(**row) if row else None

    async def get_active(self, track: str) -> Optional[GameRoom]:
        """Return the one live room for this track, preferring a running round."""
        row = await self._fetchrow(
            "SELECT * FROM game_rooms WHERE state IN ('waiting', 'running') AND track = $1 "
            "ORDER BY CASE state WHEN 'running' THEN 0 ELSE 1 END, created_at DESC LIMIT 1",
            track,
        )
        return GameRoom(**row) if row else None

    @asynccontextmanager
    async def exclusive_room_lock(self, room_id: str):
        """Cross-worker mutex for state transitions and drawing a ball."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock(hashtext($1))", f"bingo-room:{room_id}")
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", f"bingo-room:{room_id}")

    async def running_for_player(self, player_id: str) -> Optional[GameRoom]:
        row = await self._fetchrow(
            "SELECT r.* FROM game_rooms r WHERE r.state = 'running' AND EXISTS "
            "(SELECT 1 FROM cards c WHERE c.room_id = r.id AND c.owner_id = $1 AND c.funded_by <> 'unfunded') "
            "ORDER BY r.created_at DESC LIMIT 1",
            player_id,
        )
        return GameRoom(**row) if row else None

    async def list_recent(self, limit: int = 50, state_filter: Optional[str] = None) -> List[GameRoom]:
        """Most recent rounds (any state) - powers the admin panel's Games
        tab (who won, which cartela, how much)."""
        if state_filter == "waiting":
            rows = await self._fetch(
                "SELECT * FROM game_rooms WHERE state = 'waiting' ORDER BY created_at DESC LIMIT $1", limit
            )
        elif state_filter == "running":
            rows = await self._fetch(
                "SELECT * FROM game_rooms WHERE state = 'running' ORDER BY created_at DESC LIMIT $1", limit
            )
        elif state_filter == "ended":
            rows = await self._fetch(
                "SELECT * FROM game_rooms WHERE state = 'ended' AND (end_reason IS NULL OR end_reason NOT LIKE 'admin_terminated%') ORDER BY created_at DESC LIMIT $1", limit
            )
        elif state_filter == "terminated":
            rows = await self._fetch(
                "SELECT * FROM game_rooms WHERE end_reason LIKE 'admin_terminated%' ORDER BY created_at DESC LIMIT $1", limit
            )
        elif state_filter == "cancelled":
            rows = await self._fetch(
                "SELECT * FROM game_rooms WHERE state = 'cancelled' ORDER BY created_at DESC LIMIT $1", limit
            )
        else:
            rows = await self._fetch(
                "SELECT * FROM game_rooms ORDER BY created_at DESC LIMIT $1", limit
            )
        return [GameRoom(**r) for r in rows]

    async def delete(self, room_id: str) -> bool:
        """Permanently delete a game room and all associated rows from the DB."""
        await self._execute("DELETE FROM cards WHERE room_id = $1", room_id)
        await self._execute("DELETE FROM claims WHERE room_id = $1", room_id)
        await self._execute("DELETE FROM draw_logs WHERE room_id = $1", room_id)
        res = await self._execute("DELETE FROM game_rooms WHERE id = $1", room_id)
        return res != "DELETE 0"

    async def delete_waiting_rooms(self, track: Optional[str] = None) -> int:
        """Permanently delete all waiting game rooms from the DB (optionally filtered by track)."""
        query = "SELECT id FROM game_rooms WHERE state = 'waiting'"
        params = []
        if track:
            query += " AND track = $1"
            params.append(track)
        rows = await self._fetch(query, *params)
        deleted = 0
        for r in rows:
            if await self.delete(r["id"]):
                deleted += 1
        return deleted
