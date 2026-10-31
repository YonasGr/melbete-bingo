"""Shared helpers for Postgres (Supabase)-backed repositories."""
import re
from typing import Any, Optional

from src.core.db import get_pool

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class BaseRepository:
    """Base class that gives repositories a connection pool and a few
    small query helpers. Deliberately does NOT connect to anything in
    `__init__` - the pool is created lazily, on first actual query (see
    `src.core.db.get_pool`), so merely instantiating a repository (which
    several call sites do inline, e.g. `PlayerRepository().get_by_id(...)`)
    never requires a live database connection."""

    table_name: str

    async def _pool(self):
        return await get_pool()

    async def _fetchrow(self, query: str, *args: Any) -> Optional[dict]:
        pool = await self._pool()
        row = await pool.fetchrow(query, *args)
        return dict(row) if row is not None else None

    async def _fetch(self, query: str, *args: Any) -> list[dict]:
        pool = await self._pool()
        rows = await pool.fetch(query, *args)
        return [dict(r) for r in rows]

    async def _fetchval(self, query: str, *args: Any) -> Any:
        pool = await self._pool()
        return await pool.fetchval(query, *args)

    async def _execute(self, query: str, *args: Any) -> str:
        pool = await self._pool()
        return await pool.execute(query, *args)

    async def _update_fields(self, table: str, id_column: str, id_value: str, fields: dict) -> None:
        """Builds and runs `UPDATE <table> SET a = $2, b = $3, ... WHERE
        <id_column> = $1` for an arbitrary dict of {column: value} - the
        Postgres equivalent of Firestore's partial-field `.update()`.
        Field NAMES are validated against a strict identifier pattern
        (they're always literal dict keys from our own code, never raw
        user input, but this is cheap defense in depth since Postgres
        can't parameterize identifiers the way it does values)."""
        if not fields:
            return
        for key in fields:
            if not _SAFE_IDENTIFIER.match(key):
                raise ValueError(f"Unsafe column name in update: {key!r}")
        set_clause = ", ".join(f"{key} = ${i + 2}" for i, key in enumerate(fields))
        query = f"UPDATE {table} SET {set_clause} WHERE {id_column} = $1"
        await self._execute(query, id_value, *fields.values())
