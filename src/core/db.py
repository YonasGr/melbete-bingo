"""
Postgres connection pool (any standard provider - Supabase, Azure
Database for PostgreSQL, self-hosted, etc.).

This module is the single place that knows how to connect to the
database. Every repository in `src/repositories` gets its connection pool
from here.

We use `asyncpg` directly against a plain Postgres connection string
rather than any provider-specific REST client, so that:
  - queries don't add an extra HTTP hop on top of the DB round trip
    (this app is latency-sensitive - see the cartela-picking speed work
    in the history of this codebase), and
  - the async driver integrates directly with FastAPI's event loop.

If DATABASE_URL points at a transaction-mode PgBouncer pooler (Supabase's
recommended pooler is the common case), asyncpg's server-side prepared
statement cache must stay disabled - see `db_use_prepared_statements` in
src/core/config.py for when it's safe to turn back on.
"""
import json
import logging
from typing import Optional

import asyncpg

from src.core.config import settings

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Registers a JSON/JSONB codec so JSONB columns round-trip as plain
    Python dicts/lists automatically, the same shape the app already
    works with everywhere (Pydantic models, Firestore documents before
    this migration). Without this, asyncpg hands back raw JSON strings."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )


async def get_pool() -> asyncpg.Pool:
    """Returns the process-wide connection pool, creating it on first use.
    Deliberately lazy (not created at import time or in any __init__) so
    importing this module - or constructing a repository - never requires
    a live database connection; only actually running a query does."""
    global _pool
    if _pool is None:
        if not settings.database_url:
            raise RuntimeError(
                "DATABASE_URL is not set - point it at your Postgres instance "
                "(Supabase, Azure Database for PostgreSQL, self-hosted, etc). "
                "See docs/deployment.md if you're using Supabase."
            )
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            # 0 disables asyncpg's server-side prepared statement cache -
            # required if DATABASE_URL points at a transaction-mode
            # PgBouncer pooler (e.g. Supabase's pooler). Safe to enable via
            # DB_USE_PREPARED_STATEMENTS=true when connecting directly to
            # Postgres (Azure Database for PostgreSQL, self-hosted, etc.) -
            # see the setting's docstring in src/core/config.py.
            statement_cache_size=100 if settings.db_use_prepared_statements else 0,
            command_timeout=settings.db_command_timeout_seconds,
            init=_init_connection,
        )
    return _pool


async def init_db() -> None:
    """Eagerly verify database connectivity on application startup so that
    configuration errors surface immediately instead of on first request."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
        # Initialize broadcast_progress table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_progress (
                id TEXT PRIMARY KEY,
                total_users INTEGER NOT NULL DEFAULT 0,
                sent_users INTEGER NOT NULL DEFAULT 0,
                delivered_users INTEGER NOT NULL DEFAULT 0,
                failed_users INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'idle',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            INSERT INTO broadcast_progress (id, total_users, sent_users, delivered_users, failed_users, status)
            VALUES ('active', 0, 0, 0, 0, 'idle')
            ON CONFLICT (id) DO NOTHING;
        """)
    logger.info("Database pool ready")


async def close_db() -> None:
    """Cleanly closes the pool on application shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
