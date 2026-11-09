import time
from collections.abc import Callable

from llm_router.config import Settings
from llm_router.db import connect
from llm_router.errors import ErrorType, GatewayError


def wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


class SlidingWindowLimiter:
    """Sliding-log limiter: one row per admitted request, window = (now - window_ms, now]."""

    def __init__(self, settings: Settings, now_ms: Callable[[], int] = wall_clock_ms):
        self._s = settings
        self._now_ms = now_ms

    async def reserve(self, tenant: str, tokens: int) -> int:
        """Admit `tokens` for `tenant` or raise GatewayError. Returns the reservation id."""
        if tokens > self._s.limit_tokens:  # could never be admitted; a 429 would lie
            raise GatewayError(ErrorType.INVALID_REQUEST)
        now = self._now_ms()  # read once: eviction, the new row and Retry-After agree
        async with connect(self._s) as conn:
            # The race guard: the write lock is taken here, before the read. A second
            # connection blocks in BEGIN IMMEDIATE until we COMMIT or ROLLBACK, so it
            # can never sum a window that we are about to change.
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute("DELETE FROM usage WHERE ts_ms <= ?", (now - self._s.window_ms,))
                cur = await conn.execute(
                    "SELECT COALESCE(SUM(tokens), 0), MIN(ts_ms) FROM usage WHERE tenant = ?",
                    (tenant,),
                )
                used, oldest = await cur.fetchone()
                if used + tokens > self._s.limit_tokens:
                    raise GatewayError(
                        ErrorType.RATE_LIMITED, retry_after_s=self._retry_after_s(oldest, now)
                    )
                cur = await conn.execute(
                    "INSERT INTO usage (tenant, ts_ms, tokens) VALUES (?, ?, ?)",
                    (tenant, now, tokens),
                )
                await conn.execute("COMMIT")
                return cur.lastrowid
            except BaseException:  # deny, error or cancel: release the write lock now
                await conn.execute("ROLLBACK")
                raise

    def _retry_after_s(self, oldest_ts_ms: int, now: int) -> int:
        # When the oldest row leaves the window, as RFC 9110 delay-seconds (integer). Eviction
        # just removed every row with ts <= now - window, so this delay is > 0 ms, and rounding
        # up keeps it >= 1 s.
        # shortcut: oldest row may free too little, upgrade = walk rows until enough expire.
        return -(-(oldest_ts_ms + self._s.window_ms - now) // 1000)

    async def reconcile(self, reservation_id: int, actual_tokens: int) -> None:
        """Replace the estimate with what the provider reports. One statement = atomic."""
        async with connect(self._s) as conn:
            await conn.execute(
                "UPDATE usage SET tokens = ? WHERE id = ?", (actual_tokens, reservation_id)
            )

    async def release(self, reservation_id: int) -> None:
        """Failed or cancelled calls give their reservation back."""
        async with connect(self._s) as conn:
            await conn.execute("DELETE FROM usage WHERE id = ?", (reservation_id,))
