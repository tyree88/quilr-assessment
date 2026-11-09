import pytest

from mcp_gateway.auth import extract_bearer, resolve_role

TOKENS = {"tok_admin_demo": "admin", "tok_viewer_demo": "viewer"}


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Basic abc",
        "Bearer",
        "Bearer ",
        "Bearer a b",
        "Bearer tök",
        "bearer\ttok",
        "Bearer  tok",
        "Bearer x=y",
        "Bearer tok\n",
    ],
)
def test_malformed_header_yields_no_token(header):
    assert extract_bearer(header) is None


@pytest.mark.parametrize(
    ("header", "token"),
    [("Bearer tok_admin", "tok_admin"), ("bearer tok", "tok"), ("BEARER x==", "x==")],
)
def test_well_formed_header_yields_token(header, token):
    assert extract_bearer(header) == token


def test_role_lookup():
    assert resolve_role("tok_viewer_demo", TOKENS) == "viewer"
    assert resolve_role("tok_unknown", TOKENS) is None
