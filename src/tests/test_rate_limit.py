import pytest
from fastapi import HTTPException

from src.core.rate_limit import RateLimiter, enforce_rate_limit, limiter


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_allows_up_to_limit_then_blocks():
    clock = Clock()
    rl = RateLimiter(clock=clock)
    assert all(rl.check("w", "p1", 3, 60) is None for _ in range(3))
    assert rl.check("w", "p1", 3, 60) == pytest.approx(60)


def test_window_slides():
    clock = Clock()
    rl = RateLimiter(clock=clock)
    for _ in range(3):
        rl.check("w", "p1", 3, 60)
    clock.t += 61
    assert rl.check("w", "p1", 3, 60) is None


def test_keys_and_scopes_are_independent():
    rl = RateLimiter(clock=Clock())
    for _ in range(3):
        rl.check("w", "p1", 3, 60)
    assert rl.check("w", "p2", 3, 60) is None
    assert rl.check("d", "p1", 3, 60) is None


def test_memory_is_bounded():
    clock = Clock()
    rl = RateLimiter(clock=clock, max_keys=100)
    for i in range(1000):
        rl.check("w", f"p{i}", 3, 60)
    assert len(rl._hits) <= 100


def test_enforce_raises_429_with_retry_after():
    limiter.reset()
    for _ in range(3):
        enforce_rate_limit("t", "p", 3, 60)
    with pytest.raises(HTTPException) as e:
        enforce_rate_limit("t", "p", 3, 60)
    assert e.value.status_code == 429
    assert int(e.value.headers["Retry-After"]) >= 1
    limiter.reset()
