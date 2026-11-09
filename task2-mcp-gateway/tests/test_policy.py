import pytest

from mcp_gateway.jsonrpc import JsonRpcRequest
from mcp_gateway.policy import authorize

MISSING = object()


def call(name=MISSING, method="tools/call"):
    params = {} if name is MISSING else {"name": name}
    return JsonRpcRequest.model_validate(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )


ADMIN_NAMES = ["admin_reset_key", "admin_", "Admin_x", "ADMIN_X", "\uff41\uff44\uff4d\uff49\uff4e_x", "admin\uff3fx"]


@pytest.mark.parametrize("name", ADMIN_NAMES)
def test_viewer_is_denied_admin_tools_in_every_form(name):
    assert authorize("viewer", call(name)) == "deny"


@pytest.mark.parametrize("name", ADMIN_NAMES)
def test_admin_may_call_admin_tools(name):
    assert authorize("admin", call(name)) == "allow"


@pytest.mark.parametrize("name", ["get_customer", "administer", "my_admin_tool"])
def test_viewer_may_call_other_tools(name):
    assert authorize("viewer", call(name)) == "allow"


@pytest.mark.parametrize("name", [MISSING, 42, ["admin_x"], None, "\u200badmin_x", "\u00adadmin_x"])
@pytest.mark.parametrize("role", ["viewer", "admin"])
def test_unusable_name_is_invalid_params(role, name):
    assert authorize(role, call(name)) == "invalid_params"


def test_method_case_cannot_skip_the_policy():
    assert authorize("viewer", call("admin_reset_key", method="TOOLS/CALL")) == "deny"


@pytest.mark.parametrize("method", ["tools/list", "initialize", "anything/else"])
def test_other_methods_are_allowed(method):
    req = JsonRpcRequest.model_validate({"jsonrpc": "2.0", "id": 1, "method": method})
    assert authorize("viewer", req) == "allow"
