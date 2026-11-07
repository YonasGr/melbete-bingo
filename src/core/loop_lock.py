"""Postgres lease locks: at most ONE API instance runs each background loop (round draws, lobby
countdowns, the engagement job, the daily reconciliation).

Why: Azure Container Apps starts the new revision before it stops the old one, and the loops live in
process memory, so for a few seconds two instances could both run a round's draw loop. A lease is a row in
`loop_locks` that expires if its holder stops renewing, so a crashed instance never blocks anyone for long.
It is a plain table (not session advisory locks), so it also works through PgBouncer / Supabase poolers.

Fail-closed: if the lease cannot be confirmed (lost, or the database is unreachable) the caller stops its loop.
`loop_supervisor` in api/main.py then restarts it within seconds in whichever instance holds the lease."""
import logging
import os
import uuid

from src.core.db import get_pool

logger = logging.getLogger(__name__)

HOLDER = uuid.uuid4().hex          # identifies this process
_shutting_down = False
_DDL = """CREATE TABLE IF NOT EXISTS loop_locks (
    name TEXT PRIMARY KEY, holder TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL)"""

# Insert, or take over / renew only if we already hold it or the previous lease has expired.
_ACQUIRE = """INSERT INTO loop_locks (name, holder, expires_at)
VALUES ($1, $2, now() + make_interval(secs => $3))
ON CONFLICT (name) DO UPDATE SET holder = EXCLUDED.holder, expires_at = EXCLUDED.expires_at
WHERE loop_locks.holder = EXCLUDED.holder OR loop_locks.expires_at < now()
RETURNING holder"""


def _disabled() -> bool:
    return os.environ.get("MOCK_DB") == "1"


async def try_acquire(name: str, ttl_seconds: float) -> bool:
    """Acquire or renew the lease `name` for `ttl_seconds`. True only if this process now holds it."""
    if _disabled():
        return True
    if _shutting_down:
        return False
    try:
        pool = await get_pool()
        # Integration tests and deployments can bind a fresh schema or
        # connection pool to this process; CREATE IF NOT EXISTS is cheap and
        # keeps the lease table available in each search path.
        await pool.execute(_DDL)
        return await pool.fetchval(_ACQUIRE, name, HOLDER, float(ttl_seconds)) == HOLDER
    except Exception:  # noqa: BLE001
        logger.exception("Could not confirm loop lock %r - treating it as not held", name)
        return False


async def release(name: str) -> None:
    """Give the lease back so another instance can take over immediately. Only deletes our own row."""
    if _disabled():
        return
    try:
        pool = await get_pool()
        await pool.execute("DELETE FROM loop_locks WHERE name = $1 AND holder = $2", name, HOLDER)
    except Exception:  # noqa: BLE001
        logger.warning("Could not release loop lock %r (it will expire on its own)", name, exc_info=True)


async def shutdown() -> None:
    """Called on graceful shutdown: stop acquiring (so our still-running loops cannot re-take a lease
    after we hand it over) and release every lease we hold so the new instance continues at once."""
    global _shutting_down
    _shutting_down = True
    if _disabled():
        return
    try:
        pool = await get_pool()
        await pool.execute("DELETE FROM loop_locks WHERE holder = $1", HOLDER)
    except Exception:  # noqa: BLE001
        logger.warning("Could not release loop locks on shutdown (they expire on their own)", exc_info=True)
