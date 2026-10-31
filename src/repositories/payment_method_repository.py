"""Postgres repository for admin-managed payment methods."""
from typing import List, Optional

from src.models.schemas import PaymentMethod, utcnow
from src.repositories.base import BaseRepository


class PaymentMethodRepository(BaseRepository):
    table_name = "payment_methods"

    async def get(self, method_id: str) -> Optional[PaymentMethod]:
        row = await self._fetchrow("SELECT * FROM payment_methods WHERE id = $1", method_id)
        return PaymentMethod(**row) if row else None

    async def get_by_key(self, key: str) -> Optional[PaymentMethod]:
        row = await self._fetchrow("SELECT * FROM payment_methods WHERE key = $1", key)
        return PaymentMethod(**row) if row else None

    async def create(self, method: PaymentMethod) -> PaymentMethod:
        data = method.model_dump()
        await self._execute(
            """
            INSERT INTO payment_methods
                (id, key, name, account_info, account_holder, instructions,
                 active, supports_deposit, supports_withdrawal, display_order, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            data["id"], data["key"], data["name"], data["account_info"],
            data["account_holder"], data["instructions"], data["active"],
            data["supports_deposit"], data["supports_withdrawal"],
            data["display_order"], data["created_at"],
        )
        return method

    async def update(self, method_id: str, fields: dict) -> None:
        fields = {**fields, "updated_at": utcnow()}
        await self._update_fields("payment_methods", "id", method_id, fields)

    async def delete(self, method_id: str) -> None:
        await self._execute("DELETE FROM payment_methods WHERE id = $1", method_id)

    async def list_all(self) -> List[PaymentMethod]:
        rows = await self._fetch("SELECT * FROM payment_methods ORDER BY display_order ASC")
        return [PaymentMethod(**r) for r in rows]

    async def list_active(self) -> List[PaymentMethod]:
        return [m for m in await self.list_all() if m.active]
