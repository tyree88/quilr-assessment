"""A tiny MCP-shaped downstream server for the tests and the probe. Not part of the gateway."""

import json

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

TOOLS = [
    {
        "name": "get_customer",
        "description": "Look up a customer record.",
        "inputSchema": {"type": "object", "properties": {"customer_id": {"type": "string"}}},
    },
    {
        "name": "admin_reset_key",
        "description": "Rotate a customer's API key.",
        "inputSchema": {"type": "object", "properties": {"customer_id": {"type": "string"}}},
    },
]
TOOL_NAMES = {tool["name"] for tool in TOOLS}


def _answer(message: dict) -> dict:
    method = message.get("method")
    if method == "tools/list":
        return {"result": {"tools": TOOLS}}
    if method == "tools/call":
        name = (message.get("params") or {}).get("name")
        if isinstance(name, str) and name in TOOL_NAMES:
            return {"result": {"content": [{"type": "text", "text": f"{name} ok"}]}}
        # MCP: errors in finding the tool are protocol errors, not isError results.
        return {"error": {"code": -32602, "message": "Unknown tool"}}
    return {"error": {"code": -32601, "message": "Method not found"}}


def create_mock(delay: float = 0.0, broken: bool = False) -> Starlette:
    """Build a mock. `calls` counts every request received; `completed` counts finished ones."""

    async def mcp(request: Request) -> Response:
        app.state.calls += 1
        app.state.last_headers = dict(request.headers)
        if delay:
            await anyio.sleep(delay)
        app.state.completed += 1
        if broken:
            # Internal detail the gateway must never pass on to its client.
            return PlainTextResponse("Traceback: db-internal-01.corp:5432 refused", status_code=500)
        message = json.loads(await request.body())
        if "id" not in message:
            return Response(status_code=202)
        session = request.headers.get("mcp-session-id")
        headers = {"Mcp-Session-Id": session} if session else None
        body = {"jsonrpc": "2.0", "id": message["id"], **_answer(message)}
        return JSONResponse(body, headers=headers)

    async def calls(request: Request) -> Response:
        return JSONResponse({"calls": app.state.calls})

    app = Starlette(routes=[Route("/mcp", mcp, methods=["POST"]), Route("/_calls", calls)])
    app.state.calls = 0
    app.state.completed = 0
    app.state.last_headers = {}
    return app
