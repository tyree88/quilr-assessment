import httpx
import pytest

from mock_downstream.app import create_mock

pytestmark = pytest.mark.anyio


async def post(app, body, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        return await client.post("/mcp", json=body, headers=headers)


async def test_tools_list_and_counter():
    mock = create_mock()
    resp = await post(mock, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [t["name"] for t in resp.json()["result"]["tools"]] == ["get_customer", "admin_reset_key"]
    assert mock.state.calls == 1


async def test_unknown_tool_is_a_protocol_error():
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "nope"}}
    resp = await post(create_mock(), body)
    assert resp.json()["error"]["code"] == -32602


async def test_notification_gets_202_and_session_id_is_echoed():
    mock = create_mock()
    resp = await post(mock, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp.status_code == 202 and resp.content == b""
    resp = await post(
        mock, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, {"Mcp-Session-Id": "s-1"}
    )
    assert resp.headers["mcp-session-id"] == "s-1"
