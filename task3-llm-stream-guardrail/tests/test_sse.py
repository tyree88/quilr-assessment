import json

import pytest

from guardrail.sse import (
    UpstreamProtocolError,
    chunk_frame,
    delta_text,
    error_frame,
    iter_events,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def events(lines: list[str]) -> list[str]:
    async def gen():
        for line in lines:
            yield line

    return [e async for e in iter_events(gen())]


async def test_blank_line_dispatches_and_comments_are_skipped():
    assert await events([": ping", "data: a", "", ": keep-alive", "data:b", ""]) == ["a", "b"]


async def test_multi_line_data_joins_with_newline():
    assert await events(["data: one", "data: two", ""]) == ["one\ntwo"]


async def test_empty_event_and_other_fields_are_not_dispatched():
    assert await events(["", "event: x", "id: 1", "", "data:", ""]) == []


async def test_incomplete_event_at_end_is_discarded():
    assert await events(["data: a", "", "data: cut off"]) == ["a"]


def test_delta_text_shapes():
    assert delta_text('{"choices":[{"delta":{"content":"hi"}}]}') == "hi"
    assert delta_text('{"choices":[{"delta":{"role":"assistant"}}]}') == ""
    assert delta_text('{"choices":[{"delta":{},"finish_reason":"stop"}]}') == ""
    assert delta_text('{"choices":[],"usage":{"total_tokens":3}}') == ""


@pytest.mark.parametrize("data", [
    "not json", "[]", '{"x":1}', '{"choices":{}}', '{"choices":[{}]}',
    '{"choices":[{"delta":"x"}]}', '{"choices":[{"delta":{"content":5}}]}',
])
def test_delta_text_rejects_malformed(data):
    with pytest.raises(UpstreamProtocolError):
        delta_text(data)


def test_frames_are_well_formed():
    frame = chunk_frame('a "q" é')
    assert frame.startswith(b"data: ") and frame.endswith(b"\n\n")
    assert json.loads(frame[6:])["choices"][0]["delta"]["content"] == 'a "q" é'
    err = error_frame("upstream_error")
    assert err.startswith(b"event: error\ndata: ") and b"upstream_error" in err
