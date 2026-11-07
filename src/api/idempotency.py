"""Idempotent execution of money-moving requests.

Clients send `Idempotency-Key: <uuid>` (generated once per user action, reused on
retry). Semantics per (player, scope, key):

- first request       -> runs the handler; its JSON response is stored.
- same key + same body, already completed -> stored response is replayed, handler NOT run.
- same key, still running  -> 409 (client should wait/poll, not resend).
- same key, different body -> 422 (key reuse bug on the client).
- `cacheable(response)` is False -> response returned but key released (nothing moved).
- handler raised HTTPException (clean 4xx rejection, nothing moved) -> key is released so the
  user can fix the input and retry. Any other exception leaves the key in_progress until it
  goes stale (so a crash after money moved can't be blindly retried into a double charge).

The key is OPTIONAL for backward compatibility with old cached clients: without it the
handler just runs (the atomic DB operations still protect balances). Send it from every
current client.
"""
import hashlib
import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException

from src.repositories.idempotency_repository import IdempotencyRepository

logger = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^[A-Za-z0-9_\-:.]{8,128}$")
STALE_AFTER_SECONDS = 120
_active_attempts: dict[tuple[str, str, str], int] = {}


def request_fingerprint(payload: Any) -> str:
    """Stable hash of the request body (screenshots hashed like everything else)."""
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def run_idempotent(
    player_id: str,
    scope: str,
    key: Optional[str],
    payload: Any,
    handler: Callable[[], Awaitable[dict]],
    repo: Optional[IdempotencyRepository] = None,
    cacheable: Optional[Callable[[dict], bool]] = None,
) -> dict:
    if not key:
        return await handler()
    if not _KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="Invalid Idempotency-Key (8-128 chars: letters, digits, _ - : .)")

    attempt_id = (player_id, scope, key)
    _active_attempts[attempt_id] = _active_attempts.get(attempt_id, 0) + 1
    repo = repo or IdempotencyRepository()
    try:
        fingerprint = request_fingerprint(payload)
        existing = await repo.begin(player_id, scope, key, fingerprint, STALE_AFTER_SECONDS)
        if existing is not None:
            if existing["request_hash"] != fingerprint:
                raise HTTPException(status_code=422, detail="Idempotency-Key was already used with a different request.")
            if existing["status"] == "completed" and existing["response"] is not None:
                if _active_attempts[attempt_id] > 1:
                    raise HTTPException(status_code=409, detail="This request is already being processed. Please wait a moment.")
                return existing["response"]
            raise HTTPException(status_code=409, detail="This request is already being processed. Please wait a moment.")

        try:
            response = await handler()
        except HTTPException:
            await repo.release(player_id, scope, key)
            raise
        # Any other exception: leave in_progress on purpose (see module docstring).
        if cacheable is not None and not cacheable(response):
            # e.g. a bingo claim that was judged invalid: nothing moved, and the same card may
            # legitimately win on a later retry, so do not pin the "invalid" answer to this key.
            await repo.release(player_id, scope, key)
            return response
        try:
            await repo.complete(player_id, scope, key, response)
        except Exception:  # noqa: BLE001
            # Money already moved and the caller will get its response; a stuck in_progress row
            # only means a retry gets 409 until it goes stale. Never fail the request over this.
            logger.exception("Could not store idempotent response for %s/%s", scope, key)
        return response
    finally:
        remaining = _active_attempts.get(attempt_id, 1) - 1
        if remaining:
            _active_attempts[attempt_id] = remaining
        else:
            _active_attempts.pop(attempt_id, None)
