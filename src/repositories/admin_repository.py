"""Postgres repository for admin users."""
from typing import List, Optional

from src.models.schemas import AdminUser
from src.repositories.base import BaseRepository


class AdminRepository(BaseRepository):
    table_name = "admins"

    async def get_by_telegram_id(self, telegram_id: str) -> Optional[AdminUser]:
        row = await self._fetchrow(
            "SELECT * FROM admins WHERE telegram_id = $1", str(telegram_id)
        )
        return AdminUser(**row) if row else None

    async def create(self, admin: AdminUser) -> AdminUser:
        # Upsert on telegram_id (not id) - a given telegram_id can never
        # end up with two admin rows, and re-adding an existing admin just
        # refreshes their record instead of erroring.
        data = admin.model_dump()
        await self._execute(
            """
            INSERT INTO admins (id, telegram_id, is_super_admin, added_by, created_at, role, extra_permissions)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (telegram_id) DO UPDATE SET
                is_super_admin = EXCLUDED.is_super_admin,
                added_by = EXCLUDED.added_by,
                role = EXCLUDED.role,
                extra_permissions = EXCLUDED.extra_permissions
            """,
            data["id"], data["telegram_id"], data["is_super_admin"],
            data["added_by"], data["created_at"],
            "super" if data["is_super_admin"] else (data.get("role") or "admin"),
            data.get("extra_permissions") or [],
        )
        return admin

    async def delete(self, telegram_id: str) -> None:
        await self._execute("DELETE FROM admins WHERE telegram_id = $1", str(telegram_id))

    async def list_all(self) -> List[AdminUser]:
        rows = await self._fetch("SELECT * FROM admins ORDER BY created_at ASC")
        return [AdminUser(**r) for r in rows]

    async def set_alert_types(self, telegram_id: str, types) -> None:
        """Store one admin's alert subscriptions (None = back to the role defaults)."""
        await self._execute("UPDATE admins SET alert_types = $2 WHERE telegram_id = $1", str(telegram_id), types)
