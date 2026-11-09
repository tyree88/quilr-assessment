"""End to end: client -> gateway -> mock downstream, all in-process over ASGITransport."""

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from mcp_gateway.app import create_app
from mock_downstream.app import create_mock

pytestmark = pytest.mark.anyio

TOKENS = {"tok_admin_demo": "admin", "tok_viewer_demo": "viewer"}
ADMIN = {"Authorization": "Bearer tok_admin_demo"}
VIEWER = {"Authorization": "Bearer tok_viewer_demo"}


def rpc(method, params=None, id=1):
    message = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        message["id"] = id
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode()


RESET = rpc("tools/call", {"name": "admin_reset_key", "arguments": {"customer_id": "c1"}}, id=7)


@asynccontextmanager
async def gateway(mock=None, transport=None):
    mock = mock or create_mock()
    app = create_app("http://mock/mcp", TOKENS, transport=transport or httpx.ASGITransport(app=mock))
    async with app.router.lifespan_context(app):  # ASGITransport does not run lifespan itself
        transport_in = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport_in, base_url="http://gw") as client:
            yield client, mock


async def direct(body):
    transport = httpx.ASGITransport(app=create_mock())
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        return (await client.post("/mcp", content=body)).content


async def test_missing_token_is_401_with_challenge_and_no_downstream_call():
    async with gateway() as (gw, mock):
        resp = await gw.post("/mcp", content=RESET)
    assert resp.status_code == 401 and resp.headers["www-authenticate"] == "Bearer"
    assert resp.json() == {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Unauthorized"}}
    assert mock.state.calls == 0


async def test_unknown_token_and_garbage_body_is_401_not_parse_error():
    async with gateway() as (gw, mock):
        resp = await gw.post("/mcp", content=b"{", headers={"Authorization": "Bearer tok_guess"})
    assert resp.status_code == 401 and mock.state.calls == 0


async def test_viewer_admin_call_is_intercepted_and_downstream_untouched():
    async with gateway() as (gw, mock):
        resp = await gw.post("/mcp", content=RESET, headers=VIEWER)
    assert resp.status_code == 200
    assert resp.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32001, "message": "Unauthorized Tool Call"},
    }
    assert mock.state.calls == 0


async def test_viewer_admin_notification_is_403_with_no_body():
    body = rpc("tools/call", {"name": "admin_reset_key"}, id=None)
    async with gateway() as (gw, mock):
        resp = await gw.post("/mcp", content=body, headers=VIEWER)
    assert resp.status_code == 403 and resp.content == b"" and mock.state.calls == 0


async def test_admin_admin_call_is_forwarded_byte_identical():
    async with gateway() as (gw, mock):
        resp = await gw.post("/mcp", content=RESET, headers=ADMIN)
    assert resp.status_code == 200 and resp.content == await direct(RESET)
    assert mock.state.calls == 1


@pytest.mark.parametrize(
    "body",
    [rpc("tools/call", {"name": "get_customer", "arguments": {"customer_id": "c1"}}), rpc("tools/list")],
)
async def test_viewer_plain_calls_and_tools_list_are_forwarded_unmodified(body):
    async with gateway() as (gw, mock):
        resp = await gw.post("/mcp", content=body, headers=VIEWER)
    assert resp.content == await direct(body) and mock.state.calls == 1


async def test_tools_list_is_not_filtered_for_viewers():
    async with gateway() as (gw, _):
        resp = await gw.post("/mcp", content=rpc("tools/list"), headers=VIEWER)
    assert "admin_reset_key" in [tool["name"] for tool in resp.json()["result"]["tools"]]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"{", -32700),
        (b'[{"jsonrpc":"2.0","id":1,"method":"tools/list"}]', -32600),
        (b'{"jsonrpc":"1.0","id":1,"method":"tools/list"}', -32600),
        (rpc("tools/call", {"name": "\u200badmin_reset_key"}), -32602),
        (rpc("tools/call", {"name": ["admin_reset_key"]}), -32602),
    ],
)
async def test_bad_messages_are_answered_by_the_gateway(body, code):
    async with gateway() as (gw, mock):
        resp = await gw.post("/mcp", content=body, headers=ADMIN)
    assert resp.status_code == 200 and resp.json()["error"]["code"] == code
    assert mock.state.calls == 0


async def test_allowed_notification_is_forwarded_and_answered_202():
    async with gateway() as (gw, mock):
        resp = await gw.post("/mcp", content=rpc("notifications/initialized", id=None), headers=VIEWER)
    assert resp.status_code == 202 and resp.content == b"" and mock.state.calls == 1


async def test_downstream_down_is_sanitized_502():
    def refuse(request):
        raise httpx.ConnectError("Connection refused: db-internal-01.corp:9000")

    async with gateway(transport=httpx.MockTransport(refuse)) as (gw, _):
        resp = await gw.post("/mcp", content=rpc("tools/list"), headers=VIEWER)
    assert resp.status_code == 502 and resp.json()["error"]["data"] == {"reason": "unreachable"}
    assert b"corp" not in resp.content


async def test_get_is_not_allowed():
    async with gateway() as (gw, _):
        assert (await gw.get("/mcp", headers=VIEWER)).status_code == 405


def test_unknown_role_in_token_map_fails_at_startup():
    with pytest.raises(ValueError):
        create_app("http://mock/mcp", {"tok": "root"})
