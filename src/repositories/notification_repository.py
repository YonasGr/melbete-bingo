"""Postgres repository for player notifications (event messages + in-app inbox)."""
import json
from datetime import datetime
from typing import Optional, Sequence

from src.repositories.base import BaseRepository


class NotificationRepository(BaseRepository):
    table_name = "notifications"

    async def create(self, notification_id: str, player_id: str, type_: str, title: str, body: str,
                     data: dict, dedupe_key: Optional[str], created_at: datetime) -> Optional[dict]:
        """Insert one notification. Returns the row, or None if this dedupe_key already exists
        for the player (the event was already notified)."""
        return await self._fetchrow(
            """
            INSERT INTO notifications (id, player_id, type, title, body, data, dedupe_key, created_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
            ON CONFLICT DO NOTHING
            RETURNING *
            """,
            notification_id, player_id, type_, title, body, json.dumps(data or {}), dedupe_key, created_at,
        )

    async def set_telegram_status(self, notification_id: str, status: str) -> None:
        await self._execute("UPDATE notifications SET telegram_status = $2 WHERE id = $1", notification_id, status)

    async def list_for_player(self, player_id: str, limit: int = 30, before: Optional[datetime] = None) -> list[dict]:
        if before is not None:
            return await self._fetch(
                "SELECT * FROM notifications WHERE player_id = $1 AND created_at < $2 "
                "ORDER BY created_at DESC LIMIT $3", player_id, before, limit,
            )
        return await self._fetch(
            "SELECT * FROM notifications WHERE player_id = $1 ORDER BY created_at DESC LIMIT $2", player_id, limit
        )

    async def unread_count(self, player_id: str) -> int:
        return int(await self._fetchval(
            "SELECT COUNT(*) FROM notifications WHERE player_id = $1 AND read_at IS NULL", player_id
        ) or 0)

    async def mark_read(self, player_id: str, ids: Optional[Sequence[str]] = None) -> int:
        """Mark the given notifications (or ALL when ids is None) read. Only the player's own rows
        can be touched. Returns how many rows changed."""
        if ids is None:
            rows = await self._fetch(
                "UPDATE notifications SET read_at = now() WHERE player_id = $1 AND read_at IS NULL RETURNING id",
                player_id,
            )
        else:
            rows = await self._fetch(
                "UPDATE notifications SET read_at = now() WHERE player_id = $1 AND read_at IS NULL "
                "AND id = ANY($2::text[]) RETURNING id", player_id, list(ids),
            )
        return len(rows)
