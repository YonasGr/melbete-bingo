"""Postgres repository for idempotency keys (see src/api/idempotency.py)."""
from typing import Optional

import asyncpg

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
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Do not wait behind an in-flight request. Waiting would
                    # let concurrent retries observe its completed response
                    # instead of receiving the documented 409 conflict.
                    await conn.execute("SET LOCAL lock_timeout = '10ms'")
                    row = await conn.fetchrow(
                        """
                        INSERT INTO idempotency_keys (player_id, scope, key, request_hash)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (player_id, scope, key) DO UPDATE
                            SET request_hash = EXCLUDED.request_hash, updated_at = now(), created_at = now()
                            WHERE idempotency_keys.status = 'in_progress'
                              AND idempotency_keys.updated_at < now() - make_interval(secs => $5)
                              AND idempotency_keys.request_hash = EXCLUDED.request_hash
                        RETURNING player_id
                        """,
                        player_id, scope, key, request_hash, float(stale_after_seconds),
                    )
        except (asyncpg.exceptions.LockNotAvailableError, asyncpg.exceptions.QueryCanceledError):
            # The conflicting row is still owned by another transaction, so
            # its uncommitted contents are intentionally not visible here.
            return {"status": "in_progress", "request_hash": request_hash, "response": None}
        if row is not None:
            return None
        return await self._fetchrow(
            "SELECT status, request_hash, response FROM idempotency_keys "
            "WHERE player_id = $1 AND scope = $2 AND key = $3",
            player_id, scope, key,
        )

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
