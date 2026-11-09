import json

import pytest

from llm_router.errors import (
    _PUBLIC,
    ErrorPayload,
    ErrorType,
    GatewayError,
    to_response,
)


@pytest.mark.parametrize(
    ("type_", "status"),
    [
        (ErrorType.RATE_LIMITED, 429),
        (ErrorType.UPSTREAM_UNAVAILABLE, 503),
        (ErrorType.INVALID_REQUEST, 400),
        (ErrorType.INTERNAL, 500),
    ],
)
def test_status_and_shape(type_, status):
    resp = to_response(GatewayError(type_), "req-1")
    body = json.loads(resp.body)
    assert resp.status_code == status
    assert set(body) == {"error"} and set(body["error"]) == {"type", "message", "request_id"}
    assert ErrorPayload.model_validate(body).error.type is type_
    assert body["error"]["request_id"] == resp.headers["x-request-id"] == "req-1"


def test_every_type_has_public_text():
    assert set(_PUBLIC) == set(ErrorType)


def test_retry_after_header_only_when_set():
    limited = to_response(GatewayError(ErrorType.RATE_LIMITED, retry_after_s=7), "r")
    assert limited.headers["retry-after"] == "7"
    assert "retry-after" not in to_response(GatewayError(ErrorType.INTERNAL), "r").headers


def test_status_override_for_missing_key():
    err = GatewayError(ErrorType.INVALID_REQUEST, status=401)
    assert to_response(err, "r").status_code == 401
