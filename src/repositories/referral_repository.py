"""Postgres repository for referral relationships."""
from typing import List, Optional

from src.models.schemas import Referral
from src.repositories.base import BaseRepository


class ReferralRepository(BaseRepository):
    table_name = "referrals"

    async def create(self, referral: Referral) -> Referral:
        data = referral.model_dump()
        await self._execute(
            """
            INSERT INTO referrals
                (id, referrer_id, referred_id, bonus_amount, awarded, created_at, awarded_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            data["id"], data["referrer_id"], data["referred_id"], data["bonus_amount"],
            data["awarded"], data["created_at"], data["awarded_at"],
        )
        return referral

    async def update(self, referral_id: str, fields: dict) -> None:
        await self._update_fields("referrals", "id", referral_id, fields)

    async def get_by_referred_id(self, referred_id: str) -> Optional[Referral]:
        row = await self._fetchrow(
            "SELECT * FROM referrals WHERE referred_id = $1", referred_id
        )
        return Referral(**row) if row else None

    async def list_by_referrer(self, referrer_id: str) -> List[Referral]:
        rows = await self._fetch(
            "SELECT * FROM referrals WHERE referrer_id = $1 ORDER BY created_at DESC",
            referrer_id,
        )
        return [Referral(**r) for r in rows]

    async def list_all(self, limit: int = 500) -> List[Referral]:
        rows = await self._fetch(
            "SELECT * FROM referrals ORDER BY created_at DESC LIMIT $1", limit
        )
        return [Referral(**r) for r in rows]
