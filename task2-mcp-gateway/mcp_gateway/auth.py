"""Bearer token extraction and role lookup."""

import re
from collections.abc import Mapping
from typing import Literal

Role = Literal["admin", "viewer"]

# RFC 6750 §2.1: credentials = "Bearer" 1*SP b64token
_B64TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")


def extract_bearer(authorization: str | None) -> str | None:
    """Return the token, or None when the header is missing, not Bearer, or not one b64token."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not _B64TOKEN.fullmatch(token):
        return None
    return token


def resolve_role(token: str, tokens: Mapping[str, Role]) -> Role | None:
    # shortcut: static token map, swap for JWT verification (JWKS, aud, role claim) in production
    return tokens.get(token)
