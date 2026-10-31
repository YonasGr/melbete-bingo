"""Postgres repository for bingo cards."""
import logging
from typing import List, Optional

from pydantic import ValidationError

from src.models.schemas import Card
from src.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class CardRepository(BaseRepository):
    table_name = "cards"

    @staticmethod
    def _parse(row: dict) -> Optional[Card]:
        """Builds a Card from a database row, tolerating legacy/malformed
        records (e.g. missing a field added by a later schema change) by
        skipping and logging instead of raising. One bad leftover row must
        never be able to take down the whole live session for every
        player - it just gets treated as if it doesn't exist."""
        try:
            return Card(**row)
        except ValidationError:
            logger.warning("Skipping malformed card row %s", row.get("id"), exc_info=True)
            return None

    async def get(self, card_id: str) -> Optional[Card]:
        row = await self._fetchrow("SELECT * FROM cards WHERE id = $1", card_id)
        return self._parse(row) if row else None

    async def create(self, card: Card) -> Card:
        data = card.model_dump()
        await self._execute(
            """
            INSERT INTO cards (id, room_id, owner_id, variant, cartela_number, grid, funded_by, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            data["id"], data["room_id"], data["owner_id"], data["variant"],
            data["cartela_number"], data["grid"], data["funded_by"], data["created_at"],
        )
        return card

    async def list_by_room(self, room_id: str) -> List[Card]:
        rows = await self._fetch(
            "SELECT * FROM cards WHERE room_id = $1 ORDER BY created_at, id", room_id
        )
        return [c for c in (self._parse(r) for r in rows) if c is not None]

    async def list_by_room_and_owner(self, room_id: str, owner_id: str) -> List[Card]:
        rows = await self._fetch(
            "SELECT * FROM cards WHERE room_id = $1 AND owner_id = $2", room_id, owner_id
        )
        return [c for c in (self._parse(r) for r in rows) if c is not None]

    async def count_players_by_room(self, room_id: str) -> int:
        """Return the number of distinct players in a round without
        transferring every card grid.  This is used by the hot session
        endpoint, which every open Mini App periodically refreshes."""
        return await self._fetchval(
            "SELECT COUNT(DISTINCT owner_id) FROM cards WHERE room_id = $1", room_id
        )

    async def update_grid(self, card_id: str, grid: list) -> None:
        await self._execute("UPDATE cards SET grid = $2 WHERE id = $1", card_id, grid)

    async def update(self, card_id: str, fields: dict) -> None:
        await self._update_fields("cards", "id", card_id, fields)

    async def delete(self, card_id: str) -> None:
        """Used when a reserved cartela can't be charged at round start
        (e.g. the player's balance changed in the meantime) - the card and
        its cartela reservation are dropped so the round can proceed with
        whoever else joined."""
        await self._execute("DELETE FROM cards WHERE id = $1", card_id)
