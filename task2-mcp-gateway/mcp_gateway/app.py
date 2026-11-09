"""The gateway: one POST /mcp route that runs auth -> parse -> authorize -> forward."""

import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from mcp_gateway.auth import Role, extract_bearer, resolve_role
from mcp_gateway.errors import INVALID_PARAMS, UNAUTHORIZED, make_error
from mcp_gateway.forward import forward
from mcp_gateway.jsonrpc import JsonRpcRequest, parse_request
from mcp_gateway.policy import authorize

ROLES = ("admin", "viewer")


def create_app(
    downstream_url: str,
    tokens: Mapping[str, Role],
    *,
    timeout: float = 10.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    """Build the gateway. `transport` lets tests route the downstream call in-process."""
    if any(role not in ROLES for role in tokens.values()):
        raise ValueError("token map holds a role other than admin or viewer")

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # One shared client (one connection pool) for the gateway's lifetime.
        # timeout=None because forward() owns the deadline with anyio.fail_after.
        async with httpx.AsyncClient(transport=transport, timeout=None) as client:
            app.state.client = client
            yield

    async def mcp(request: Request) -> Response:
        # 1. Authenticate first, so an anonymous caller learns nothing about parsing.
        token = extract_bearer(request.headers.get("authorization"))
        role = resolve_role(token, tokens) if token else None
        if role is None:
            return make_error(
                None, UNAUTHORIZED, "Unauthorized", status=401, headers={"WWW-Authenticate": "Bearer"}
            )
        # 2. Parse: never forward bytes that are not one valid JSON-RPC message.
        body = await request.body()
        parsed = parse_request(body)
        if not isinstance(parsed, JsonRpcRequest):
            return parsed
        # 3. Authorize before any network I/O, for requests and notifications alike.
        decision = authorize(role, parsed)
        if decision != "allow" and parsed.is_notification:
            return Response(status_code=403 if decision == "deny" else 400)  # no body, per spec
        if decision == "deny":
            return make_error(parsed.id, UNAUTHORIZED, "Unauthorized Tool Call")
        if decision == "invalid_params":
            return make_error(parsed.id, INVALID_PARAMS, "Invalid params")
        # 4. Forward the original bytes; pass the answer through or sanitize the failure.
        client = request.app.state.client
        return await forward(client, downstream_url, request.headers, body, parsed, timeout)

    return Starlette(routes=[Route("/mcp", mcp, methods=["POST"])], lifespan=lifespan)


def app_from_env() -> Starlette:
    """uvicorn --factory entry point: GATEWAY_DOWNSTREAM_URL, GATEWAY_TOKENS (JSON), GATEWAY_TIMEOUT_S."""
    return create_app(
        os.environ["GATEWAY_DOWNSTREAM_URL"],
        json.loads(os.environ["GATEWAY_TOKENS"]),
        timeout=float(os.environ.get("GATEWAY_TIMEOUT_S", "10")),
    )
