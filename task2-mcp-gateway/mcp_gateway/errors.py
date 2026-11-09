"""JSON-RPC error codes and the one function that builds an error response."""

from starlette.responses import JSONResponse

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# The task brief requires -32001. MCP 2026-07-28 marks -32000..-32019 as legacy; see README.
UNAUTHORIZED = -32001
HEADER_MISMATCH = -32020

RequestId = int | str | None


def make_error(
    request_id: RequestId,
    code: int,
    message: str,
    *,
    status: int = 200,
    data: dict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    body = {"jsonrpc": "2.0", "id": request_id, "error": error}
    return JSONResponse(body, status_code=status, headers=headers)
