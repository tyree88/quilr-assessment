import json

import pytest

from mcp_gateway.jsonrpc import JsonRpcRequest, parse_request


def error_of(result):
    body = json.loads(result.body)
    return body["id"], body["error"]["code"]


def test_valid_body_becomes_a_request():
    req = parse_request(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
    assert isinstance(req, JsonRpcRequest) and req.id == 1


@pytest.mark.parametrize(
    "body",
    [b"{", b"", b"\xff", b"NaN", b'{"a":Infinity}', b"[" * 100_000, b"1" + b"0" * 5000],
)
def test_unparseable_body_is_a_parse_error(body):
    assert error_of(parse_request(body)) == (None, -32700)


@pytest.mark.parametrize(
    "body",
    [
        b'[{"jsonrpc":"2.0","id":1,"method":"a"},{"jsonrpc":"2.0","id":2,"method":"b"}]',
        b"3",
        b'"tools/list"',
        (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            b'"params":{"name":"admin_reset_key","name":"get_customer"}}'
        ),
        b'{"jsonrpc":"2.0","id":1,"id":2,"method":"tools/list"}',
    ],
)
def test_non_object_batch_or_duplicate_key_is_invalid_request(body):
    assert error_of(parse_request(body)) == (None, -32600)


def test_invalid_envelope_echoes_a_readable_id():
    assert error_of(parse_request(b'{"jsonrpc":"1.0","id":7,"method":"x"}')) == (7, -32600)


def test_unusable_id_is_not_echoed():
    assert error_of(parse_request(b'{"jsonrpc":"2.0","id":true,"method":"x"}')) == (None, -32600)
