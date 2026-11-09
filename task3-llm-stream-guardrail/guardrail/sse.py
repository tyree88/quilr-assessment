"""Server-sent events in and out.

Parsing follows WHATWG HTML §9.2.6 (https://html.spec.whatwg.org/multipage/server-sent-events.html):
a blank line dispatches the event, lines starting with ":" are comments, "data" values are
joined with "\\n", one leading space after the colon is dropped, an event with empty data is
not dispatched, and an incomplete event at end of stream is discarded.
Chunk shape follows openai-python `ChatCompletionChunk`: choices[i].delta.content: str | None.
"""

import json
from collections.abc import AsyncIterator

DONE = b"data: [DONE]\n\n"


class UpstreamProtocolError(Exception):
    """The upstream sent something that is not an OpenAI-style chunk."""


async def iter_events(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield the data of each dispatched event from already-split lines."""
    data: str | None = None
    async for line in lines:
        if not line:
            if data:  # "data" was seen and is non-empty
                yield data
            data = None
            continue
        field, _, value = line.partition(":")  # a ":comment" line gets field "" and is ignored
        if field == "data":
            value = value.removeprefix(" ")
            data = value if data is None else f"{data}\n{value}"
    # end of stream: a pending event without its blank line is discarded (spec)


def delta_text(data: str) -> str:
    """Text content of one chunk; "" for role-only, finish or usage chunks."""
    try:
        chunk = json.loads(data)
        choices = chunk["choices"]
        if not isinstance(choices, list):
            raise TypeError
        if not choices:
            return ""
        content = choices[0]["delta"].get("content")
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        raise UpstreamProtocolError("malformed chunk") from exc
    if content is None:
        return ""
    if not isinstance(content, str):
        raise UpstreamProtocolError("content is not a string")
    return content


def chunk_frame(text: str) -> bytes:
    body = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": text}}]}
    return f"data: {json.dumps(body, separators=(',', ':'))}\n\n".encode()


def error_frame(code: str) -> bytes:
    """Fixed codes only: never carries upstream text, hostnames or exception messages."""
    body = {"error": {"code": code, "message": "upstream stream failed"}}
    return f"event: error\ndata: {json.dumps(body, separators=(',', ':'))}\n\n".encode()
