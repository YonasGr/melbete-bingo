"""Postgres repository for idempotency keys (see src/api/idempotency.py)."""
import json
from typing import Optional

from src.repositories.base import BaseRepository


class IdempotencyRepository(BaseRepository):
    table_name = "idempotency_keys"

    async def begin(self, player_id: str, scope: str, key: str, request_hash: str, stale_after_seconds: int) -> Optional[dict]:
        """Claim the key. Returns None if THIS call now owns it (go execute), otherwise the
        existing row (dict) so the caller can replay / reject.

        A stale in_progress row (owner crashed mid-request) is taken over atomically.
        The conditional UPDATE + ON CONFLICT means exactly one concurrent caller wins.
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Serialize claims for this logical key. This also protects
                # installations whose pre-existing table was created without
                # the expected primary-key constraint.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    json.dumps([player_id, scope, key], separators=(",", ":")),
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO idempotency_keys (player_id, scope, key, request_hash)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (player_id, scope, key) DO NOTHING
                    RETURNING player_id
                    """,
                    player_id, scope, key, request_hash,
                )
                if row is not None:
                    return None

                row = await conn.fetchrow(
                    """
                    UPDATE idempotency_keys
                    SET request_hash = $4, updated_at = now(), created_at = now()
                    WHERE player_id = $1 AND scope = $2 AND key = $3
                      AND status = 'in_progress'
                      AND updated_at < now() - make_interval(secs => $5)
                      AND request_hash = $4
                    RETURNING player_id
                    """,
                    player_id, scope, key, request_hash, float(stale_after_seconds),
                )
                if row is not None:
                    return None

                return dict(await conn.fetchrow(
                    "SELECT status, request_hash, response FROM idempotency_keys "
                    "WHERE player_id = $1 AND scope = $2 AND key = $3",
                    player_id, scope, key,
                ))

    async def complete(self, player_id: str, scope: str, key: str, response: dict) -> None:
        await self._execute(
            "UPDATE idempotency_keys SET status = 'completed', response = $4, updated_at = now() "
            "WHERE player_id = $1 AND scope = $2 AND key = $3",
            player_id, scope, key, response,
        )

    async def release(self, player_id: str, scope: str, key: str) -> None:
        """Forget an in-progress key so the client may retry (used after a clean rejection)."""
        await self._execute(
            "DELETE FROM idempotency_keys WHERE player_id = $1 AND scope = $2 AND key = $3 "
            "AND status = 'in_progress'",
            player_id, scope, key,
        )

    async def purge_older_than(self, days: int) -> int:
        result = await self._execute(
            "DELETE FROM idempotency_keys WHERE created_at < now() - make_interval(days => $1)", days
        )
        return int(result.split()[-1]) if result else 0
