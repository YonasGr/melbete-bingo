"""Postgres repository for LinkCode documents - the short-lived codes that
link a web-only session to a real Telegram account."""
from typing import Optional

from src.models.schemas import LinkCode
from src.repositories.base import BaseRepository


class LinkCodeRepository(BaseRepository):
    table_name = "link_codes"

    async def create(self, link_code: LinkCode) -> LinkCode:
        data = link_code.model_dump()
        await self._execute(
            """
            INSERT INTO link_codes
                (id, code, web_player_id, status, telegram_id, linked_player_id,
                 session_token, created_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            data["id"], data["code"], data["web_player_id"], data["status"],
            data["telegram_id"], data["linked_player_id"], data["session_token"],
            data["created_at"], data["expires_at"],
        )
        return link_code

    async def get(self, link_code_id: str) -> Optional[LinkCode]:
        row = await self._fetchrow("SELECT * FROM link_codes WHERE id = $1", link_code_id)
        return LinkCode(**row) if row else None

    async def get_by_code(self, code: str) -> Optional[LinkCode]:
        row = await self._fetchrow("SELECT * FROM link_codes WHERE code = $1", code)
        return LinkCode(**row) if row else None

    async def update(self, link_code_id: str, fields: dict) -> None:
        await self._update_fields("link_codes", "id", link_code_id, fields)
