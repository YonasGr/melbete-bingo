"""Idempotency semantics, tested against an in-memory repository."""
import asyncio

import pytest
from fastapi import HTTPException

from src.api.idempotency import request_fingerprint, run_idempotent


class FakeIdemRepo:
    def __init__(self):
        self.rows = {}

    async def begin(self, player_id, scope, key, request_hash, stale_after_seconds):
        k = (player_id, scope, key)
        if k in self.rows:
            return dict(self.rows[k])
        self.rows[k] = {"status": "in_progress", "request_hash": request_hash, "response": None}
        return None

    async def complete(self, player_id, scope, key, response):
        self.rows[(player_id, scope, key)].update(status="completed", response=response)

    async def release(self, player_id, scope, key):
        if self.rows.get((player_id, scope, key), {}).get("status") == "in_progress":
            del self.rows[(player_id, scope, key)]


KEY = "withdraw-1234567890"


@pytest.mark.asyncio
async def test_retry_replays_response_without_rerunning_handler():
    repo, calls = FakeIdemRepo(), []

    async def handler():
        calls.append(1)
        return {"transaction_id": "t1"}

    r1 = await run_idempotent("p", "withdraw", KEY, {"amount": 200}, handler, repo=repo)
    r2 = await run_idempotent("p", "withdraw", KEY, {"amount": 200}, handler, repo=repo)
    assert r1 == r2 == {"transaction_id": "t1"}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_runs_handler_once():
    repo, calls = FakeIdemRepo(), []

    async def handler():
        calls.append(1)
        await asyncio.sleep(0.01)
        return {"ok": True}

    results = await asyncio.gather(
        *[run_idempotent("p", "withdraw", KEY, {"a": 1}, handler, repo=repo) for _ in range(10)],
        return_exceptions=True,
    )
    assert len(calls) == 1
    assert sum(1 for r in results if r == {"ok": True}) == 1
    assert all(isinstance(r, HTTPException) and r.status_code == 409 for r in results if r != {"ok": True})


@pytest.mark.asyncio
async def test_same_key_different_body_is_422():
    repo = FakeIdemRepo()

    async def handler():
        return {"ok": True}

    await run_idempotent("p", "withdraw", KEY, {"amount": 200}, handler, repo=repo)
    with pytest.raises(HTTPException) as e:
        await run_idempotent("p", "withdraw", KEY, {"amount": 999}, handler, repo=repo)
    assert e.value.status_code == 422


@pytest.mark.asyncio
async def test_clean_rejection_releases_key_so_user_can_retry():
    repo, n = FakeIdemRepo(), {"i": 0}

    async def handler():
        n["i"] += 1
        if n["i"] == 1:
            raise HTTPException(status_code=400, detail="bad amount")
        return {"ok": True}

    with pytest.raises(HTTPException):
        await run_idempotent("p", "withdraw", KEY, {"a": 1}, handler, repo=repo)
    assert await run_idempotent("p", "withdraw", KEY, {"a": 1}, handler, repo=repo) == {"ok": True}


@pytest.mark.asyncio
async def test_unexpected_crash_keeps_key_blocked():
    repo = FakeIdemRepo()

    async def handler():
        raise RuntimeError("db died after commit?")

    with pytest.raises(RuntimeError):
        await run_idempotent("p", "withdraw", KEY, {"a": 1}, handler, repo=repo)

    async def ok():
        return {"ok": True}

    with pytest.raises(HTTPException) as e:
        await run_idempotent("p", "withdraw", KEY, {"a": 1}, ok, repo=repo)
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_uncacheable_response_is_not_pinned():
    repo, n = FakeIdemRepo(), {"i": 0}

    async def handler():
        n["i"] += 1
        return {"valid": n["i"] > 1}

    kw = dict(cacheable=lambda r: r["valid"], repo=repo)
    assert (await run_idempotent("p", "claim:r1", "claim-card-00001", {"c": 1}, handler, **kw))["valid"] is False
    assert (await run_idempotent("p", "claim:r1", "claim-card-00001", {"c": 1}, handler, **kw))["valid"] is True
    # now pinned: replay, handler not run again
    assert (await run_idempotent("p", "claim:r1", "claim-card-00001", {"c": 1}, handler, **kw))["valid"] is True
    assert n["i"] == 2


@pytest.mark.asyncio
async def test_no_key_just_runs_and_bad_key_is_400():
    async def handler():
        return {"ok": True}

    assert await run_idempotent("p", "w", None, {}, handler) == {"ok": True}
    with pytest.raises(HTTPException) as e:
        await run_idempotent("p", "w", "x y", {}, handler, repo=FakeIdemRepo())
    assert e.value.status_code == 400


def test_fingerprint_is_order_independent():
    assert request_fingerprint({"a": 1, "b": 2}) == request_fingerprint({"b": 2, "a": 1})
