"""Lease locks against real Postgres: exactly one holder at a time, takeover after expiry or release."""
import asyncio

import pytest

import src.core.loop_lock as ll

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _as(holder: str, name: str, ttl: float) -> bool:
    previous, ll.HOLDER = ll.HOLDER, holder
    try:
        return await ll.try_acquire(name, ttl)
    finally:
        ll.HOLDER = previous


async def test_only_one_holder_and_renewal(pg):
    assert await _as("A", "draw:r1", 30) is True
    assert await _as("B", "draw:r1", 30) is False      # held by A
    assert await _as("A", "draw:r1", 30) is True       # A renews


async def test_takeover_after_expiry_and_release(pg):
    assert await _as("A", "draw:r2", 0.2) is True
    await asyncio.sleep(0.4)
    assert await _as("B", "draw:r2", 30) is True       # expired -> B takes over
    previous, ll.HOLDER = ll.HOLDER, "B"
    try:
        await ll.release("draw:r2")
    finally:
        ll.HOLDER = previous
    assert await _as("C", "draw:r2", 30) is True       # released -> immediately available


async def test_concurrent_acquire_has_single_winner(pg):
    results = await asyncio.gather(*[_as(f"H{i}", "draw:r3", 30) for i in range(20)])
    assert results.count(True) == 1


async def test_shutdown_stops_reacquiring(pg):
    ll._shutting_down = False
    assert await ll.try_acquire("draw:r4", 30) is True
    await ll.shutdown()
    try:
        assert await ll.try_acquire("draw:r4", 30) is False
        assert await _as("B", "draw:r4", 30) is True   # our lease was handed over
    finally:
        ll._shutting_down = False
