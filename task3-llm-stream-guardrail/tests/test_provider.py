import json

import httpx
import pytest

from guardrail.provider import DEMO, MockUpstream, script

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_mock_streams_frames_and_records_close():
    upstream = MockUpstream({"demo": (200, script("a", "b")), "x": (503, [b"busy"])})
    async with httpx.AsyncClient(transport=upstream) as client:
        async with client.stream("POST", "http://up/v1", json={"model": "demo"}) as resp:
            lines = [line async for line in resp.aiter_lines()]
        assert resp.status_code == 200 and upstream.last.closed
        datas = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
        assert [d["choices"][0]["delta"]["content"] for d in datas] == ["a", "b"]
        assert "data: [DONE]" in lines
        resp = await client.post("http://up/v1", json={"model": "x"})
        assert resp.status_code == 503


def test_demo_scenario_exists():
    assert DEMO["demo"][0] == 200
