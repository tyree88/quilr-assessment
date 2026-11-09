import asyncio
import logging

import httpx
import pytest

from llm_router.app import MAX_BODY_BYTES, create_app
from llm_router.config import Settings
from llm_router.db import connect, init_db
from llm_router.errors import ErrorPayload
from llm_router.providers import FakeProvider

pytestmark = pytest.mark.anyio

KEY = {"X-API-Key": "tenant-key-1"}
BODY = {"prompt": "hello there", "max_tokens": 600}


@pytest.fixture
async def settings(tmp_path):
    s = Settings(db_path=tmp_path / "gw.db", limit_tokens=1_000, upstream_timeout_s=0.2)
    await init_db(s)
    return s


def client_for(settings, primary, secondary=None):
    app = create_app(settings, primary, secondary or FakeProvider("s"))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


async def window(settings) -> list[int]:
    async with connect(settings) as conn:
        return [r[0] for r in await (await conn.execute("SELECT tokens FROM usage")).fetchall()]


def error_of(resp) -> dict:
    payload = ErrorPayload.model_validate(resp.json())  # exact contract, nothing extra
    assert payload.error.request_id == resp.headers["x-request-id"]
    return resp.json()["error"]


async def test_success_reconciles_to_actual_tokens(settings):
    async with client_for(settings, FakeProvider("p", tokens=42)) as c:
        resp = await c.post("/v1/completions", json=BODY, headers=KEY)
    assert resp.status_code == 200
    assert resp.json() == {"text": "p says hi", "provider": "primary", "usage": {"total_tokens": 42}}
    assert resp.headers["x-request-id"]
    assert await window(settings) == [42]


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": ""}, {"X-API-Key": "   "}])
async def test_missing_key_is_401(settings, headers):
    async with client_for(settings, FakeProvider("p")) as c:
        resp = await c.post("/v1/completions", json=BODY, headers=headers)
    assert resp.status_code == 401 and error_of(resp)["type"] == "invalid_request"


@pytest.mark.parametrize("content", [b"{not json", b'{"prompt": "x", "extra": 1}', b"[]"])
async def test_bad_body_is_400(settings, content):
    async with client_for(settings, FakeProvider("p")) as c:
        resp = await c.post("/v1/completions", content=content, headers=KEY)
    assert resp.status_code == 400 and error_of(resp)["type"] == "invalid_request"


async def test_gateway_rate_limit_is_429_with_retry_after(settings):
    async with client_for(settings, FakeProvider("p", tokens=600)) as c:
        assert (await c.post("/v1/completions", json=BODY, headers=KEY)).status_code == 200
        resp = await c.post("/v1/completions", json=BODY, headers=KEY)
    assert resp.status_code == 429 and error_of(resp)["type"] == "rate_limited"
    assert 1 <= int(resp.headers["retry-after"]) <= 60


async def test_provider_429_is_never_surfaced(settings):
    async with client_for(settings, FakeProvider("p", "429")) as c:
        resp = await c.post("/v1/completions", json=BODY, headers=KEY)
    assert resp.status_code == 200 and resp.json()["provider"] == "secondary"


async def test_both_fail_is_503_and_releases_quota(settings):
    async with client_for(settings, FakeProvider("p", "429"), FakeProvider("s", "500")) as c:
        resp = await c.post("/v1/completions", json=BODY, headers=KEY)
    assert resp.status_code == 503 and error_of(resp)["type"] == "upstream_unavailable"
    assert await window(settings) == []


async def test_unknown_error_is_masked_but_logged(settings, caplog):
    with caplog.at_level(logging.INFO, logger="llm_router"):
        async with client_for(settings, FakeProvider("p", "boom")) as c:
            resp = await c.post("/v1/completions", json=BODY, headers=KEY)
    assert resp.status_code == 500 and error_of(resp)["type"] == "internal"
    assert "10.0.0.5" not in resp.text and "Traceback" not in resp.text
    rid = resp.json()["error"]["request_id"]
    assert f"request_id={rid}" in caplog.text and "10.0.0.5" in caplog.text
    assert await window(settings) == []


@pytest.mark.parametrize(("method", "path", "status"), [("POST", "/nope", 404), ("GET", "/v1/completions", 405)])
async def test_routing_errors_use_the_same_payload(settings, method, path, status):
    async with client_for(settings, FakeProvider("p")) as c:
        resp = await c.request(method, path)
    assert resp.status_code == status and error_of(resp)["type"] == "invalid_request"


async def test_client_cancel_releases_quota_and_cancels_primary(settings):
    primary = FakeProvider("p", delay_s=30)
    s = settings.model_copy(update={"upstream_timeout_s": 10.0})
    async with client_for(s, primary) as c:
        task = asyncio.create_task(c.post("/v1/completions", json=BODY, headers=KEY))
        await asyncio.sleep(0.2)
        assert await window(settings) == [603]   # ceil(11 chars / 4) + 600, reserved in flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert primary.cancelled == 1
    assert await window(settings) == []


async def test_oversized_body_is_413_by_content_length(settings):
    async with client_for(settings, FakeProvider("p")) as c:
        resp = await c.post("/v1/completions", content=b"x" * (MAX_BODY_BYTES + 1), headers=KEY)
    assert resp.status_code == 413 and error_of(resp)["type"] == "invalid_request"


async def test_oversized_chunked_body_is_413_while_streaming(settings):
    async def chunks():
        for _ in range(MAX_BODY_BYTES // 65_536 + 2):
            yield b"x" * 65_536

    async with client_for(settings, FakeProvider("p")) as c:
        resp = await c.post("/v1/completions", content=chunks(), headers=KEY)
    assert resp.status_code == 413 and error_of(resp)["type"] == "invalid_request"


async def test_largest_valid_prompt_fits_under_the_cap(settings):
    body = '{"prompt": "' + "\\u00e9" * 200_000 + '", "max_tokens": 1}'
    s = settings.model_copy(update={"limit_tokens": 60_000})
    async with client_for(s, FakeProvider("p")) as c:
        resp = await c.post("/v1/completions", content=body.encode(), headers=KEY)
    assert resp.status_code == 200
