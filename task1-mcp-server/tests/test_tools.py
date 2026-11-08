"""The two tools through a real MCP client session (in-process, no subprocess)."""

from types import SimpleNamespace

import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

from mcp_server import REQUEST_MODELS, mcp, reject_malformed_arguments

pytestmark = pytest.mark.anyio

VALID = {"customer_id": "CUST-00001", "amount": 40.0, "reason": "Damaged on arrival"}


async def test_every_registered_tool_has_a_request_model():
    # REQUEST_MODELS is the allowlist: a tool missing from it would be rejected as unknown.
    async with Client(mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == set(REQUEST_MODELS)


@pytest.mark.parametrize(
    ("params", "bad_field"),
    [
        ({"name": ["get_customer_record"], "arguments": {}}, "name"),  # unhashable name once raised TypeError
        ({"name": "get_customer_record", "arguments": []}, "arguments"),  # falsy non-object is not "missing"
    ],
)
async def test_middleware_rejects_odd_shapes_directly(params, bad_field):
    async def call_next(ctx):
        raise AssertionError("must not reach the SDK")

    with pytest.raises(MCPError) as exc:
        await reject_malformed_arguments(SimpleNamespace(method="tools/call", params=params), call_next)
    assert exc.value.code == INVALID_PARAMS
    assert bad_field in {d["field"] for d in exc.value.data}


@pytest.mark.parametrize(
    ("tool", "arguments", "bad_field"),
    [
        ("get_customer_record", {"customer_id": "cust-00001"}, "customer_id"),
        ("get_customer_record", {}, "customer_id"),
        ("get_customer_record", None, "customer_id"),
        ("trigger_refund", {**VALID, "amount": -5}, "amount"),
        ("trigger_refund", {**VALID, "amount": "40"}, "amount"),
        ("trigger_refund", {**VALID, "reason": "too short"}, "reason"),
        ("trigger_refund", {**VALID, "approved_by": "agent"}, "approved_by"),
        ("trigger_refund", {**VALID, "amount": 0.005}, "amount"),
        ("delete_customer", {"customer_id": "CUST-00001"}, "name"),
    ],
)
async def test_malformed_arguments_are_jsonrpc_invalid_params(tool, arguments, bad_field):
    async with Client(mcp) as client:
        with pytest.raises(MCPError) as exc:
            await client.call_tool(tool, arguments)
    assert exc.value.code == INVALID_PARAMS
    assert bad_field in {d["field"] for d in exc.value.data}


async def test_error_data_does_not_echo_caller_values():
    async with Client(mcp) as client:
        with pytest.raises(MCPError) as exc:
            await client.call_tool("trigger_refund", {**VALID, "reason": "secret"})
    assert "secret" not in str(exc.value.data)


async def test_unknown_customer_is_tool_error():
    async with Client(mcp) as client:
        result = await client.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
    assert result.is_error
    assert "No customer with id CUST-99999" in result.content[0].text


async def test_refund_over_balance_is_tool_error_and_balance_unchanged():
    async with Client(mcp) as client:
        result = await client.call_tool("trigger_refund", {**VALID, "amount": 400.0})
        assert result.is_error
        assert "exceeds refundable balance 250.00" in result.content[0].text
        record = await client.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
    assert record.structured_content["balance"] == 250.0


async def test_valid_refund_returns_structured_receipt():
    async with Client(mcp) as client:
        result = await client.call_tool("trigger_refund", {**VALID, "reason": "  Damaged on arrival  "})
    assert not result.is_error
    receipt = result.structured_content
    assert receipt["remaining_balance"] == 210.0
    assert receipt["reason"] == "Damaged on arrival"  # stripped
    assert receipt["refund_id"].startswith("RF-")


async def test_input_schema_advertises_constraints():
    async with Client(mcp) as client:
        tools = {t.name: t.input_schema for t in (await client.list_tools()).tools}
    refund = tools["trigger_refund"]["properties"]
    assert tools["get_customer_record"]["properties"]["customer_id"]["pattern"] == r"^CUST-[0-9]{5}$"
    assert refund["amount"]["exclusiveMinimum"] == 0
    assert refund["reason"]["minLength"] == 10
    assert all(schema["additionalProperties"] is False for schema in tools.values())
