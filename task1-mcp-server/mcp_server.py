"""Customer-ops MCP server over stdio.

Stdout carries only JSON-RPC. The SDK logs to stderr, and this module
prints nothing, at import time or later.
"""

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS
from pydantic import ValidationError

import store
from models import Amount, CustomerId, GetCustomerRequest, Reason, RefundRequest

REQUEST_MODELS = {"get_customer_record": GetCustomerRequest, "trigger_refund": RefundRequest}


async def reject_malformed_arguments(ctx, call_next):
    """Run before the SDK's own argument check, so format errors become JSON-RPC -32602, not isError."""
    if ctx.method == "tools/call" and isinstance(ctx.params, dict):
        model = REQUEST_MODELS.get(ctx.params.get("name"))
        if model is not None:
            try:
                model.model_validate(ctx.params.get("arguments") or {})
            except ValidationError as exc:
                # Field names and rule messages only: never echo the caller's values back.
                details = [
                    {"field": ".".join(map(str, err["loc"])) or "arguments", "error": err["msg"]}
                    for err in exc.errors()
                ]
                raise MCPError(code=INVALID_PARAMS, message="Invalid params", data=details) from None
    return await call_next(ctx)


mcp = MCPServer("customer-ops", version="0.1.0", log_level="ERROR", middleware=[reject_malformed_arguments])


@mcp.tool()
def get_customer_record(customer_id: CustomerId) -> store.Customer:
    """Look up one customer by id (format CUST-XXXXX)."""
    customer = store.get_customer(customer_id)
    if customer is None:
        raise ToolError(f"No customer with id {customer_id}")
    return customer


@mcp.tool()
def trigger_refund(customer_id: CustomerId, amount: Amount, reason: Reason) -> store.Refund:
    """Refund a customer. Fails if the amount is more than the refundable balance."""
    customer = store.get_customer(customer_id)
    if customer is None:
        raise ToolError(f"No customer with id {customer_id}")
    try:
        return store.record_refund(customer, amount, reason)
    except store.InsufficientBalance as exc:
        raise ToolError(str(exc)) from exc


# Advertise what the middleware enforces (extra="forbid"): the SDK's generated schema omits it.
# shortcut: reaches into the private tool manager; replace if the SDK adds a public schema hook.
for _name in REQUEST_MODELS:
    mcp._tool_manager.get_tool(_name).parameters["additionalProperties"] = False


if __name__ == "__main__":
    mcp.run(transport="stdio")
