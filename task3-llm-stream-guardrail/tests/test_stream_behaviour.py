"""Behaviour httpx.ASGITransport cannot show: it buffers the whole response (httpx 0.28,
_transports/asgi.py) and only sends http.disconnect after the body ends. These tests call
the ASGI app directly with uvicorn's spec version (2.3), so timing and disconnects are real.
"""

import json
import time
import tracemalloc

import anyio
import httpx
import pytest

from guardrail.app import create_app
from guardrail.provider import MockUpstream, script

pytestmark = pytest.mark.anyio
URL = "http://up/v1/chat/completions"
BODY = json.dumps({"model": "demo", "messages": [{"role": "user", "content": "hi"}], "stream": True})
SCOPE = {
    "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
    "method": "POST", "scheme": "http", "path": "/v1/chat/completions",
    "raw_path": b"/v1/chat/completions", "query_string": b"", "root_path": "",
    "headers": [(b"content-type", b"application/json")], "server": ("gw", 80), "client": ("t", 1),
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def call(app, on_body=None, disconnect_after=None):
    """Drive one request. Returns the number of non-empty body messages sent."""
    gone = anyio.Event()
    sent = {"bodies": 0, "requested": False}

    async def receive():
        if not sent["requested"]:
            sent["requested"] = True
            return {"type": "http.request", "body": BODY.encode(), "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.body" and msg.get("body"):
            sent["bodies"] += 1
            if on_body:
                on_body(msg["body"])
            if disconnect_after and sent["bodies"] >= disconnect_after:
                gone.set()

    with anyio.fail_after(30):
        await app(SCOPE, receive, send)
    return sent["bodies"]


def gateway(steps, delay=0.0):
    upstream = MockUpstream({"demo": (200, steps)}, delay=delay)
    return upstream, create_app(httpx.AsyncClient(transport=upstream), URL)


async def test_client_disconnect_closes_the_upstream_mid_stream():
    upstream, app = gateway(script(*[f"word{i} " for i in range(500)]), delay=0.005)
    bodies = await call(app, disconnect_after=3)
    assert bodies < 10  # stopped right away, not after 500 frames
    assert upstream.last.closed and upstream.last.sent < 100


async def test_first_byte_arrives_after_one_token_not_after_k_chars():
    delay = 0.02
    _, app = gateway(script(*[f"tok{i} " for i in range(30)]), delay=delay)
    start, firsts = time.monotonic(), []
    await call(app, on_body=lambda b: firsts.append(time.monotonic() - start))
    # "tok0 " -> "tok" goes out at once; only "0 " is held (it could start a spaced card).
    # A plain K-hold would wait for 128 chars, about 25 of these tokens.
    assert firsts[0] < 3 * delay + 0.05
    assert len(firsts) >= 25  # one client frame per upstream token: no batching


async def test_100k_token_stream_stays_in_bounded_memory():
    steps = script(*["ok joe@example.com 4111 1111 1111 1111 " if i % 50 == 0 else "word "
                     for i in range(100_000)])
    upstream, app = gateway(steps)
    seen = {"redacted": 0}

    def count(b):
        seen["redacted"] += b.count(b"[REDACTED]")

    tracemalloc.start()
    bodies = await call(app, on_body=count)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert upstream.last.sent == 100_001 and bodies > 90_000
    assert seen["redacted"] == 2 * 2_000
    assert peak < 10 * 1024 * 1024, peak
