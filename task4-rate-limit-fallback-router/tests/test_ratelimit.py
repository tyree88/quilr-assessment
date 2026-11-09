import asyncio
import sqlite3

import pytest

from llm_router.config import Settings
from llm_router.db import connect, init_db
from llm_router.errors import ErrorType, GatewayError
from llm_router.ratelimit import SlidingWindowLimiter

pytestmark = pytest.mark.anyio


class FakeClock:
    def __init__(self, start_ms: int = 1_700_000_000_000):
        self.ms = start_ms

    def __call__(self) -> int:
        return self.ms

    def advance(self, ms: int) -> None:
        self.ms += ms


@pytest.fixture
async def settings(tmp_path):
    s = Settings(db_path=tmp_path / "gw.db")
    await init_db(s)
    return s


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def limiter(settings, clock):
    return SlidingWindowLimiter(settings, now_ms=clock)


async def rows(settings) -> int:
    async with connect(settings) as conn:
        return (await (await conn.execute("SELECT COUNT(*) FROM usage")).fetchone())[0]


async def denied(limiter, tenant, tokens) -> GatewayError:
    with pytest.raises(GatewayError) as exc:
        await limiter.reserve(tenant, tokens)
    return exc.value


async def test_window_slides_and_evicts(limiter, clock, settings):
    await limiter.reserve("t", 30_000)            # t = 0
    clock.advance(30_000)
    await limiter.reserve("t", 20_000)            # t = 30 s, window now full
    clock.advance(29_000)
    err = await denied(limiter, "t", 1)           # t = 59 s
    assert err.type is ErrorType.RATE_LIMITED
    assert err.retry_after_s == 1                 # t=0 row leaves at 60 s
    clock.advance(2_000)
    await limiter.reserve("t", 1)                 # t = 61 s: t=0 row evicted
    assert await rows(settings) == 2


async def test_exact_limit_allowed_one_over_denied(limiter):
    await limiter.reserve("t", 49_000)
    await limiter.reserve("t", 1_000)
    assert (await denied(limiter, "t", 1)).type is ErrorType.RATE_LIMITED


async def test_window_boundary_is_half_open(limiter, clock):
    await limiter.reserve("t", 50_000)
    clock.advance(59_999)
    await denied(limiter, "t", 1)                 # row 59.999 s old still counts
    clock.advance(1)
    await limiter.reserve("t", 50_000)            # exactly 60 s old: out


async def test_tenants_are_independent(limiter):
    await limiter.reserve("a", 50_000)
    await limiter.reserve("b", 50_000)


async def test_request_larger_than_limit_is_invalid_not_rate_limited(limiter, settings):
    assert (await denied(limiter, "t", 50_001)).type is ErrorType.INVALID_REQUEST
    assert await rows(settings) == 0


async def test_retry_after_rounds_up(limiter, clock):
    await limiter.reserve("t", 50_000)
    clock.advance(10_500)                         # oldest leaves in 49.5 s
    assert (await denied(limiter, "t", 1)).retry_after_s == 50


async def test_eviction_covers_idle_tenants(limiter, clock, settings):
    for i in range(20):
        await limiter.reserve(f"idle-{i}", 10)
    clock.advance(60_000)
    await limiter.reserve("active", 10)
    assert await rows(settings) == 1


async def test_concurrent_race_admits_exactly_the_limit(limiter, settings):
    # 50 coroutines, 2,000 tokens each, limit 50,000: exactly 25 must win.
    results = await asyncio.gather(
        *(limiter.reserve("t", 2_000) for _ in range(50)), return_exceptions=True
    )
    wins = [r for r in results if isinstance(r, int)]
    losses = [r for r in results if isinstance(r, GatewayError)]
    assert len(wins) == 25 and len(losses) == 25
    assert all(e.type is ErrorType.RATE_LIMITED for e in losses)
    assert await rows(settings) == 25


async def test_reconcile_replaces_estimate(limiter):
    rid = await limiter.reserve("t", 5_000)
    await limiter.reconcile(rid, 1_200)
    await limiter.reserve("t", 48_800)            # 1,200 + 48,800 = 50,000: fits
    await denied(limiter, "t", 1)


async def test_release_returns_quota(limiter, settings):
    rid = await limiter.reserve("t", 50_000)
    await limiter.release(rid)
    assert await rows(settings) == 0
    await limiter.reserve("t", 50_000)


async def test_reconcile_rejects_negative_actual(limiter):
    rid = await limiter.reserve("t", 10)
    with pytest.raises(sqlite3.IntegrityError):
        await limiter.reconcile(rid, -1)


async def test_cancel_during_commit_propagates_cancel(settings, clock, monkeypatch):
    import llm_router.ratelimit as rl

    real_connect = rl.connect

    class CancelAfterCommit:
        """The COMMIT lands in the worker thread, then the cancel reaches the coroutine."""

        def __init__(self, cm):
            self._cm = cm

        async def __aenter__(self):
            self._conn = await self._cm.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._cm.__aexit__(*exc)

        @property
        def in_transaction(self):
            return self._conn.in_transaction

        async def execute(self, sql, *args):
            cur = await self._conn.execute(sql, *args)
            if sql == "COMMIT":
                raise asyncio.CancelledError
            return cur

    monkeypatch.setattr(rl, "connect", lambda s: CancelAfterCommit(real_connect(s)))
    with pytest.raises(asyncio.CancelledError):
        await SlidingWindowLimiter(settings, now_ms=clock).reserve("t", 1_000)
    assert await rows(settings) == 1  # committed but unowned: expires after one window
