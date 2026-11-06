"""Sliding-window rate limiter for money- and game-affecting endpoints.

In-process by design: production runs ONE `api` container (see
docker-compose.production.yml), so a per-process limiter is exact. If you scale
the API to several instances, swap `_hits` for Redis (INCR + EXPIRE, or a sorted
set) behind the same `RateLimiter.check` interface - callers do not change.

Keys should be an authenticated player id where possible (so one player's retries
never lock out another) and a client IP only for pre-auth endpoints like admin login.
"""
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional, Tuple

from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000):
        self._clock = clock
        self._max_keys = max_keys
        self._hits: Dict[Tuple[str, str], Deque[float]] = {}

    def check(self, scope: str, key: str, limit: int, window_seconds: float) -> Optional[float]:
        """Record one attempt. Returns None if allowed, else seconds until a slot frees up."""
        now = self._clock()
        bucket_key = (scope, key)
        hits = self._hits.get(bucket_key)
        if hits is None:
            if len(self._hits) >= self._max_keys:
                self._evict(now, window_seconds)
            hits = self._hits[bucket_key] = deque()
        cutoff = now - window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return max(hits[0] + window_seconds - now, 0.001)
        hits.append(now)
        return None

    def _evict(self, now: float, window_seconds: float) -> None:
        """Drop buckets whose newest hit is outside the window (bounds memory)."""
        stale = [k for k, h in self._hits.items() if not h or h[-1] <= now - window_seconds]
        for k in stale:
            del self._hits[k]
        if len(self._hits) >= self._max_keys:  # still full of live keys: drop oldest-inserted half
            for k in list(self._hits)[: self._max_keys // 2]:
                del self._hits[k]

    def reset(self) -> None:
        self._hits.clear()


limiter = RateLimiter()


def enforce_rate_limit(scope: str, key: str, limit: int, window_seconds: float) -> None:
    """Raise HTTP 429 (with Retry-After) if `key` exceeded `limit` hits per window for `scope`."""
    retry_after = limiter.check(scope, key, limit, window_seconds)
    if retry_after is not None:
        seconds = int(retry_after) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Please wait {seconds}s and try again.",
            headers={"Retry-After": str(seconds)},
        )


def client_ip(request: Request) -> str:
    """Best-effort client IP. The API container is only reachable through Caddy, which
    sets X-Forwarded-For itself, so the first hop is the real client."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# Central place to tune limits: scope -> (max requests, window seconds).
LIMITS: Dict[str, Tuple[int, float]] = {
    "claim": (8, 10),          # bingo claims; legit play needs 1-2
    "deposit": (5, 60),
    "agent_apply": (3, 3600),
    "withdraw": (3, 60),
    "bonus_claim": (5, 60),
    "select_cartela": (30, 10),
    "admin_login": (5, 60),    # per IP
    "agent_topup": (20, 60),   # an agent serving a queue of players
    "agent_cashout": (3, 60),
    "rematch": (10, 60),
    "notify_prefs": (10, 60),
}


def enforce(scope: str, key: str) -> None:
    limit, window = LIMITS[scope]
    enforce_rate_limit(scope, key, limit, window)
