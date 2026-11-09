import json

import httpx
import pytest

from guardrail.app import create_app
from guardrail.provider import STALL, MockUpstream, script, sse_chunk

pytestmark = pytest.mark.anyio
URL = "http://up/v1/chat/completions"
REQ = {"model": "demo", "messages": [{"role": "user", "content": "hi"}], "stream": True}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def gateway(scenarios, stall_s=1.0, delay=0.0):
    upstream = MockUpstream(scenarios, delay=delay)
    app = create_app(httpx.AsyncClient(transport=upstream), URL, stall_s=stall_s)
    return upstream, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


def parse(body: bytes) -> tuple[str, list[str]]:
    """(joined content, ordered event kinds) from a client SSE body."""
    text, kinds = "", []
    for frame in body.decode().split("\n\n"):
        if not frame:
            continue
        if frame.startswith("event: error"):
            kinds.append("error:" + json.loads(frame.split("data: ", 1)[1])["error"]["code"])
        elif frame == "data: [DONE]":
            kinds.append("done")
        else:
            assert frame.startswith("data: ")
            text += json.loads(frame[6:])["choices"][0]["delta"]["content"]
            kinds.append("chunk")
    return text, kinds


async def post(scenarios, body=REQ, **kw):
    upstream, client = gateway(scenarios, **kw)
    async with client:
        resp = await client.post("/v1/chat/completions", json=body)
    return upstream, resp


async def test_redacts_split_pii_and_sends_done_last():
    up, resp = await post({"demo": (200, script("mail jo", "hn@exa", "mple.com, ssn 123-", "45-6789 ok"))})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache" and resp.headers["x-accel-buffering"] == "no"
    text, kinds = parse(resp.content)
    assert text == "mail [REDACTED], ssn [REDACTED] ok"
    assert kinds[-1] == "done" and kinds.count("done") == 1 and up.last.closed


async def test_keepalives_role_and_finish_chunks_are_skipped():
    steps = [b": ping\n\n", b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
             *script("hello", done=False),
             b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n', b"data: [DONE]\n\n"]
    _, resp = await post({"demo": (200, steps)})
    assert parse(resp.content) == ("hello", ["chunk", "done"])


@pytest.mark.parametrize("steps, code", [
    ([sse_chunk("john@exa"), httpx.ReadError("conn reset by 10.0.0.7")], "upstream_error"),
    ([sse_chunk("john@exa"), STALL], "upstream_timeout"),
    ([sse_chunk("john@exa"), b"data: {not json\n\n"], "upstream_protocol_error"),
    ([sse_chunk("john@exa")], "upstream_truncated"),
])
async def test_mid_stream_failure_ends_with_error_and_drops_held_text(steps, code):
    up, resp = await post({"demo": (200, steps)}, stall_s=0.05)
    text, kinds = parse(resp.content)
    assert kinds[-1] == f"error:{code}" and "done" not in kinds
    assert "john" not in text and b"10.0.0.7" not in resp.content and up.last.closed


async def test_upstream_error_status_is_a_sanitized_502():
    _, resp = await post({"demo": (500, [b"stack trace at db-7.internal"])})
    assert resp.status_code == 502 and resp.json() == {"error": {"code": "upstream_error"}}


@pytest.mark.parametrize("body, code", [
    ({**REQ, "stream": False}, "stream_required"),
    ({**REQ, "n": 2}, "invalid_request"),
    ({**REQ, "messages": []}, "invalid_request"),
    ({**REQ, "stream": "yes"}, "invalid_request"),
])
async def test_bad_requests_are_400(body, code):
    _, resp = await post({"demo": (200, script("x"))}, body=body)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == code
    assert "yes" not in resp.text


async def test_oversized_body_is_413():
    big = {**REQ, "messages": [{"role": "user", "content": "x" * 1_100_000}]}
    _, resp = await post({"demo": (200, script("x"))}, body=big)
    assert resp.status_code == 413
