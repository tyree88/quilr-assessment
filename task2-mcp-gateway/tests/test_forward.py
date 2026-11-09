import json

import httpx
import pytest

from mcp_gateway.forward import forward
from mcp_gateway.jsonrpc import JsonRpcRequest
from mock_downstream.app import create_mock

pytestmark = pytest.mark.anyio

URL = "http://mock/mcp"
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
    "authorization": "Bearer tok_admin_demo",
    "connection": "upgrade",
    "te": "trailers",
    "host": "gateway.example",
    "transfer-encoding": "chunked",
    "mcp-session-id": "s-1",
    "mcp-protocol-version": "2026-07-28",
}


async def send(app_or_transport, message, timeout=5.0):
    transport = (
        app_or_transport
        if isinstance(app_or_transport, httpx.AsyncBaseTransport)
        else httpx.ASGITransport(app=app_or_transport)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        request = JsonRpcRequest.model_validate(message)
        return await forward(client, URL, HEADERS, json.dumps(message).encode(), request, timeout)


async def test_answer_passes_through_byte_identical_with_session_header():
    mock = create_mock()
    resp = await send(mock, LIST)
    transport = httpx.ASGITransport(app=create_mock())
    async with httpx.AsyncClient(transport=transport) as client:
        direct = await client.post(URL, json=LIST, headers={"mcp-session-id": "s-1"})
    assert resp.status_code == 200 and resp.body == direct.content
    assert resp.headers["mcp-session-id"] == "s-1"


async def test_only_allowlisted_headers_reach_downstream():
    mock = create_mock()
    await send(mock, LIST)
    seen = mock.state.last_headers
    assert "authorization" not in seen and "transfer-encoding" not in seen
    assert seen["host"] == "mock" and seen.get("connection") != "upgrade" and "te" not in seen
    assert seen["mcp-session-id"] == "s-1" and seen["mcp-protocol-version"] == "2026-07-28"


async def test_downstream_jsonrpc_error_passes_through():
    message = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}}
    resp = await send(create_mock(), message)
    assert json.loads(resp.body)["error"] == {"code": -32602, "message": "Unknown tool"}


def failure_of(resp):
    body = json.loads(resp.body)
    # The whole error object, so any extra field (e.g. exception text) fails the test.
    assert body["error"] == {
        "code": -32603,
        "message": "Upstream unavailable",
        "data": {"reason": body["error"]["data"]["reason"]},
    }
    return resp.status_code, body["id"], body["error"]["code"], body["error"]["data"]["reason"]


async def test_unreachable_downstream_is_sanitized():
    def refuse(request):
        raise httpx.ConnectError("[Errno 61] Connection refused: db-internal-01.corp:9000")

    resp = await send(httpx.MockTransport(refuse), LIST)
    assert failure_of(resp) == (502, 1, -32603, "unreachable")
    assert b"corp" not in resp.body and b"Errno" not in resp.body


async def test_slow_downstream_times_out_and_is_cancelled():
    mock = create_mock(delay=1.0)
    resp = await send(mock, LIST, timeout=0.05)
    assert failure_of(resp) == (504, 1, -32603, "timeout")
    assert mock.state.calls == 1 and mock.state.completed == 0


async def test_non_jsonrpc_5xx_is_sanitized():
    resp = await send(create_mock(broken=True), LIST)
    assert failure_of(resp) == (502, 1, -32603, "bad_response")
    assert b"db-internal" not in resp.body


async def test_notification_failure_has_no_body():
    resp = await send(create_mock(broken=True), {"jsonrpc": "2.0", "method": "notifications/x"})
    assert resp.status_code == 502 and resp.body == b""
