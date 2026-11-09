import httpx
import pytest

from llm_router.models import CompletionRequest
from llm_router.providers import HttpProvider, ProviderError

pytestmark = pytest.mark.anyio

REQ = CompletionRequest(prompt="hi", max_tokens=5)


def provider(handler) -> HttpProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpProvider("p", "http://upstream.internal/v1/complete", client)


async def test_ok_body_parsed_and_extra_fields_ignored():
    def ok(request):
        return httpx.Response(200, json={"text": "x", "usage": {"total_tokens": 7}, "id": "z"})

    resp = await provider(ok).complete(REQ)
    assert (resp.text, resp.total_tokens) == ("x", 7)


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (429, {}),
        (429, {"Retry-After": "120"}),
        (429, {"Retry-After": "Fri, 31 Dec 1999 23:59:59 GMT"}),
        (429, {"Retry-After": "99999999999999999999"}),
        (429, {"Retry-After": "soon"}),
        (503, {}),
        (400, {}),
    ],
)
async def test_non_200_becomes_provider_error_with_status(status, headers):
    p = provider(lambda r: httpx.Response(status, headers=headers, text="boom at 10.0.0.5"))
    with pytest.raises(ProviderError) as exc:
        await p.complete(REQ)
    assert exc.value.status == status


@pytest.mark.parametrize(
    "body", [b"not json", b'{"text": "x"}', b'{"text": "x", "usage": {"total_tokens": -1}}']
)
async def test_malformed_200_is_a_502(body):
    p = provider(lambda r: httpx.Response(200, content=body))
    with pytest.raises(ProviderError) as exc:
        await p.complete(REQ)
    assert exc.value.status == 502


async def test_connection_error_has_no_status():
    def refuse(request):
        raise httpx.ConnectError("connection refused to 10.0.0.5")

    with pytest.raises(ProviderError) as exc:
        await provider(refuse).complete(REQ)
    assert exc.value.status is None
