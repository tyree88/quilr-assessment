"""Method-level authorization: who may call which tool."""

import unicodedata
from typing import Literal

from mcp_gateway.auth import Role
from mcp_gateway.jsonrpc import JsonRpcRequest

Decision = Literal["allow", "deny", "invalid_params"]


def _fold(text: str) -> str:
    # NFKC folds fullwidth and other compatibility forms; casefold folds case.
    return unicodedata.normalize("NFKC", text).casefold()


def is_admin_tool(name: str) -> bool:
    return _fold(name).startswith("admin_")


def authorize(role: Role, request: JsonRpcRequest) -> Decision:
    """Allow everything except a non-admin calling an admin_ tool; reject unusable tool names."""
    if _fold(request.method) != "tools/call":
        return "allow"
    name = (request.params or {}).get("name")
    # NFKC keeps format characters (category Cf, e.g. U+200B), so a name holding one is rejected.
    if not isinstance(name, str) or any(unicodedata.category(c) == "Cf" for c in name):
        return "invalid_params"
    if is_admin_tool(name) and role != "admin":
        return "deny"
    return "allow"
