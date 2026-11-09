import pytest
from pydantic import ValidationError

from mcp_gateway.jsonrpc import JsonRpcRequest

BASE = {"jsonrpc": "2.0", "method": "tools/call", "id": 1}


def test_request_keeps_id_and_params():
    req = JsonRpcRequest.model_validate({**BASE, "params": {"name": "get_customer"}})
    assert req.id == 1 and not req.is_notification
    assert req.params == {"name": "get_customer"}


def test_missing_id_is_a_notification():
    req = JsonRpcRequest.model_validate({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert req.is_notification


@pytest.mark.parametrize(
    "override",
    [
        {"jsonrpc": "1.0"},
        {"method": 5},
        {"id": None},  # MCP forbids a null id
        {"id": True},  # bool is an int subclass; strict mode rejects it
        {"id": 1.0},  # MCP ids are string or integer
        {"params": [1]},  # MCP params are an object
        {"extra": 1},  # unknown top-level member
    ],
)
def test_invalid_envelopes_are_rejected(override):
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate({**BASE, **override})


def test_missing_method_is_rejected():
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate({"jsonrpc": "2.0", "id": 1})
