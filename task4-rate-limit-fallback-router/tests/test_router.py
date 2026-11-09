import asyncio
import logging
import time

import pytest

from llm_router.errors import ErrorType, GatewayError
from llm_router.models import CompletionRequest
from llm_router.providers import FakeProvider
from llm_router.router import ModelRouter

pytestmark = pytest.mark.anyio

REQ = CompletionRequest(prompt="hi", max_tokens=5)


def pair(primary_mode="ok", secondary_mode="ok", *, primary_delay=0.0, timeout_s=0.2):
    p = FakeProvider("p", primary_mode, delay_s=primary_delay)
    s = FakeProvider("s", secondary_mode)
    return p, s, ModelRouter(p, s, timeout_s)


async def test_primary_ok_secondary_never_called():
    p, s, router = pair()
    _, role = await router.complete(REQ, "r")
    assert role == "primary" and (p.calls, s.calls) == (1, 0)


@pytest.mark.parametrize("mode", ["429", "500", "conn"])
async def test_fails_over_once_without_retrying_primary(mode):
    p, s, router = pair(mode)
    _, role = await router.complete(REQ, "r")
    assert role == "secondary" and (p.calls, s.calls) == (1, 1)


async def test_other_4xx_does_not_fail_over():
    _, s, router = pair("400")
    with pytest.raises(GatewayError) as exc:
        await router.complete(REQ, "r")
    assert exc.value.type is ErrorType.INVALID_REQUEST and s.calls == 0


async def test_both_fail_is_upstream_unavailable():
    _, _, router = pair("429", "500")
    with pytest.raises(GatewayError) as exc:
        await router.complete(REQ, "r")
    assert exc.value.type is ErrorType.UPSTREAM_UNAVAILABLE


async def test_unknown_exception_propagates_for_the_app_to_mask():
    _, s, router = pair("boom")
    with pytest.raises(RuntimeError):
        await router.complete(REQ, "r")
    assert s.calls == 0


async def test_hang_times_out_at_3s_cancels_primary_then_secondary():
    p, _, router = pair(primary_delay=30, timeout_s=3.0)
    tasks_before = asyncio.all_tasks()
    start = time.monotonic()
    _, role = await router.complete(REQ, "r")
    elapsed = time.monotonic() - start
    assert role == "secondary"
    assert 2.9 < elapsed < 3.3
    assert p.cancelled == 1                       # the hung call was cancelled, not leaked
    assert asyncio.all_tasks() == tasks_before    # no task left running


async def test_outside_cancel_propagates_and_is_not_a_failover():
    p, s, router = pair(primary_delay=30, timeout_s=10)
    task = asyncio.create_task(router.complete(REQ, "r"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert p.cancelled == 1 and s.calls == 0


async def test_failure_detail_goes_to_log_with_request_id(caplog):
    _, _, router = pair("429")
    with caplog.at_level(logging.WARNING, logger="llm_router"):
        await router.complete(REQ, "req-123")
    assert "request_id=req-123" in caplog.text and "10.0.0.5" in caplog.text
