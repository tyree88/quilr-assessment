import json

from mcp_gateway.errors import INVALID_REQUEST, UNAUTHORIZED, make_error


def test_envelope_echoes_id_and_omits_absent_data():
    resp = make_error(7, INVALID_REQUEST, "Invalid Request")
    assert resp.status_code == 200
    assert json.loads(resp.body) == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32600, "message": "Invalid Request"},
    }


def test_status_headers_and_data_are_carried():
    resp = make_error(
        None,
        UNAUTHORIZED,
        "Unauthorized",
        status=401,
        headers={"WWW-Authenticate": "Bearer"},
        data={"reason": "x"},
    )
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    body = json.loads(resp.body)
    assert body["id"] is None
    assert body["error"] == {"code": -32001, "message": "Unauthorized", "data": {"reason": "x"}}
