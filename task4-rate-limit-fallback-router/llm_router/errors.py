from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse


class ErrorType(StrEnum):
    RATE_LIMITED = "rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    INVALID_REQUEST = "invalid_request"
    INTERNAL = "internal"


# Fixed public text per type. Nothing caller- or provider-supplied is ever interpolated.
_PUBLIC = {
    ErrorType.RATE_LIMITED: (429, "Token rate limit exceeded for this API key."),
    ErrorType.UPSTREAM_UNAVAILABLE: (503, "No model provider could complete the request."),
    ErrorType.INVALID_REQUEST: (400, "The request was rejected."),
    ErrorType.INTERNAL: (500, "Internal gateway error."),
}


class GatewayError(Exception):
    """The only exception type that reaches the HTTP layer as a non-500. No message field:
    upstream text has nowhere to go."""

    def __init__(
        self, type: ErrorType, *, status: int | None = None, retry_after_s: int | None = None
    ):
        super().__init__(type.value)
        self.type = type
        self.status = status or _PUBLIC[type][0]
        self.retry_after_s = retry_after_s


class _ErrorBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: ErrorType
    message: str
    request_id: str


class ErrorPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    error: _ErrorBody


def to_response(err: GatewayError, request_id: str) -> JSONResponse:
    body = ErrorPayload(
        error=_ErrorBody(type=err.type, message=_PUBLIC[err.type][1], request_id=request_id)
    )
    headers = {"X-Request-ID": request_id}
    if err.retry_after_s is not None:
        headers["Retry-After"] = str(err.retry_after_s)
    return JSONResponse(body.model_dump(mode="json"), status_code=err.status, headers=headers)
