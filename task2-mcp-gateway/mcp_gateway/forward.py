"""Forward one JSON-RPC message downstream and pass the answer back, sanitizing failures."""

import json
from collections.abc import Mapping

import anyio
import httpx
from starlette.responses import Response

from mcp_gateway.errors import INTERNAL_ERROR, make_error
from mcp_gateway.jsonrpc import JsonRpcRequest


def _pass_headers(headers: Mapping[str, str]) -> dict[str, str]:
    # An allowlist, not a hop-by-hop denylist: httpx and Starlette rebuild Host, Connection,
    # Transfer-Encoding and Content-Length, and Authorization is never passed on, because the
    # gateway's token is not a credential for the downstream (no token passthrough).
    return {
        name: value
        for name, value in headers.items()
        if name.lower() in ("content-type", "accept") or name.lower().startswith("mcp-")
    }


def _is_jsonrpc_response(content: bytes) -> bool:
    try:
        message = json.loads(content)
    except ValueError:
        return False
    return (
        isinstance(message, dict)
        and message.get("jsonrpc") == "2.0"
        and ("result" in message or "error" in message)
    )


def _failure(request: JsonRpcRequest, status: int, reason: str) -> Response:
    if request.is_notification:
        return Response(status_code=status)  # a notification never gets a body
    data = {"reason": reason}  # a fixed word, never exception text (it can name hosts)
    return make_error(request.id, INTERNAL_ERROR, "Upstream unavailable", status=status, data=data)


async def forward(
    client: httpx.AsyncClient,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    request: JsonRpcRequest,
    timeout: float,
) -> Response:
    """Send the original bytes downstream; pass a valid answer through, or return 502/504."""
    try:
        # fail_after, not httpx's timeout: it cancels the whole call on any transport,
        # including the in-process ASGITransport, which ignores httpx timeouts.
        with anyio.fail_after(timeout):
            upstream = await client.post(url, content=body, headers=_pass_headers(headers))
    except (TimeoutError, httpx.TimeoutException):
        return _failure(request, 504, "timeout")
    except httpx.HTTPError:
        return _failure(request, 502, "unreachable")

    passthrough = (
        (upstream.status_code == 202 and not upstream.content)  # accepted notification
        # shortcut: SSE is buffered, not streamed; upgrade to StreamingResponse for progress events
        or upstream.headers.get("content-type", "").startswith("text/event-stream")
        or _is_jsonrpc_response(upstream.content)
    )
    if not passthrough:
        return _failure(request, 502, "bad_response")
    return Response(
        upstream.content, status_code=upstream.status_code, headers=_pass_headers(upstream.headers)
    )
