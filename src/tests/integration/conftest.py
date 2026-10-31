"""Fixtures for tests that need a REAL Postgres (row locks cannot be faked in memory).

Set TEST_DATABASE_URL to run them, e.g.
    docker run -d --rm -p 5432:5432 -e POSTGRES_PASSWORD=test postgres:16
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres pytest src/tests/integration

Each test session gets its own throwaway schema (dropped afterwards), so it is safe to
point at any scratch database. NEVER point it at production.
"""
import os
import pathlib
import uuid

import asyncpg
import pytest
import pytest_asyncio

import src.core.db as db

DSN = os.environ.get("TEST_DATABASE_URL")
SCHEMA_SQL = pathlib.Path(__file__).resolve().parents[3] / "supabase" / "schema.sql"


def pytest_collection_modifyitems(config, items):
    if DSN:
        return
    if os.environ.get("REQUIRE_INTEGRATION_DB"):
        raise pytest.UsageError("REQUIRE_INTEGRATION_DB is set but TEST_DATABASE_URL is not")
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set - real-Postgres tests skipped")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest_asyncio.fixture
async def pg():
    """A pool bound to a fresh schema with the project's schema.sql applied; installed as the
    app-wide pool so repositories (which call get_pool()) use it."""
    schema = f"t_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        DSN, min_size=2, max_size=40, init=db._init_connection,
        server_settings={"search_path": schema}, statement_cache_size=0,
    )
    async with pool.acquire() as c:
        await c.execute(SCHEMA_SQL.read_text())
    previous, db._pool = db._pool, pool
    try:
        yield pool
    finally:
        db._pool = previous
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
