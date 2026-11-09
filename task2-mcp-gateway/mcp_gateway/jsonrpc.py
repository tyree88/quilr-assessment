"""The JSON-RPC 2.0 request envelope, validated at the trust boundary."""

import json
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)
from starlette.responses import JSONResponse

from mcp_gateway.errors import INVALID_REQUEST, PARSE_ERROR, RequestId, make_error


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    jsonrpc: Literal["2.0"]
    method: StrictStr
    id: StrictInt | StrictStr | None = None  # absent = notification
    params: dict[str, Any] | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_not_null(cls, value: object) -> object:
        # MCP 2026-07-28: "Unlike base JSON-RPC, the ID MUST NOT be null."
        if value is None:
            raise ValueError("id must not be null")
        return value

    @property
    def is_notification(self) -> bool:
        return self.id is None


class _DuplicateKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    # Parsers disagree on which duplicate wins, so a repeated key could let the
    # gateway authorize one tool name while the downstream runs another.
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKey(key)
        obj[key] = value
    return obj


def _reject_constant(name: str) -> None:
    raise ValueError(name)  # NaN / Infinity are not JSON


def _echo_id(raw: dict) -> RequestId:
    request_id = raw.get("id")
    return request_id if type(request_id) in (int, str) else None


def parse_request(body: bytes) -> JsonRpcRequest | JSONResponse:
    """Decode JSON (else -32700), require one object (else -32600), validate the envelope (else -32600)."""
    # shortcut: no body size cap, add one (Content-Length check or reverse proxy) before production
    try:
        raw = json.loads(
            body, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
    except _DuplicateKey:
        return make_error(None, INVALID_REQUEST, "Invalid Request")
    except (ValueError, RecursionError):
        return make_error(None, PARSE_ERROR, "Parse error")
    if not isinstance(raw, dict):  # a batch array or a bare scalar
        return make_error(None, INVALID_REQUEST, "Invalid Request")
    try:
        return JsonRpcRequest.model_validate(raw)
    except ValidationError:
        return make_error(_echo_id(raw), INVALID_REQUEST, "Invalid Request")
