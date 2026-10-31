"""Postgres repository for bingo claims (win verification records)."""
from datetime import datetime, timedelta, timezone

from src.models.schemas import Claim
from src.repositories.base import BaseRepository


class ClaimRepository(BaseRepository):
    table_name = "claims"

    async def list_recent_winner_ids(self, within_seconds: int) -> set:
        """Player IDs that have an accepted (i.e. actually won) claim
        within the last `within_seconds`, across every room/track.

        Used purely as a *tie-break* signal (see the auto-claim tie-break
        in main.py's draw loop) - never to block a genuine win outright,
        only to avoid handing every close simultaneous-completion race to
        whoever happened to win most recently.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
        rows = await self._fetch(
            "SELECT DISTINCT player_id FROM claims WHERE status = 'accepted' AND verified_at > $1",
            cutoff,
        )
        return {r["player_id"] for r in rows}

    async def create(self, claim: Claim) -> Claim:
        data = claim.model_dump()
        await self._execute(
            """
            INSERT INTO claims
                (id, room_id, player_id, card_id, claimed_pattern, status,
                 verification_message, created_at, verified_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            data["id"], data["room_id"], data["player_id"], data["card_id"],
            data["claimed_pattern"], data["status"], data["verification_message"],
            data["created_at"], data["verified_at"],
        )
        return claim
