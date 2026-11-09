"""POST /v1/chat/completions: stream an upstream completion back with PII redacted.

Run: uv run uvicorn guardrail.app:app  (mock upstream unless UPSTREAM_URL is set)
"""

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from typing import Literal

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from guardrail.provider import DEMO, MockUpstream
from guardrail.redactor import StreamRedactor
from guardrail.sse import (
    DONE,
    UpstreamProtocolError,
    chunk_frame,
    delta_text,
    error_frame,
    iter_events,
)

log = logging.getLogger("guardrail")

MAX_BODY = 1_000_000  # bytes; request bodies are prompts, not uploads
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """The subset of the OpenAI request we forward. One choice only: n > 1 is rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(min_length=1, max_length=128)
    messages: list[Message] = Field(min_length=1)
    stream: bool
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)


def _error(status: int, code: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, **extra}}, status_code=status)


async def _read_body(request: Request) -> bytes | None:
    body = b""
    async for part in request.stream():
        body += part
        if len(body) > MAX_BODY:
            return None
    return body


async def _lines(resp: httpx.Response, stall_s: float) -> AsyncIterator[str]:
    """Upstream lines; a gap longer than stall_s raises TimeoutError (any transport)."""
    it = resp.aiter_lines()
    while True:
        with anyio.fail_after(stall_s):
            try:
                line = await anext(it)
            except StopAsyncIteration:
                return
        yield line


async def relay(resp: httpx.Response, stall_s: float) -> AsyncIterator[bytes]:
    """Upstream SSE -> StreamRedactor -> client SSE. Holds at most K chars, never the response."""
    redactor = StreamRedactor()
    code = "upstream_truncated"  # used if the stream ends without [DONE]
    try:
        async for data in iter_events(_lines(resp, stall_s)):
            if data == "[DONE]":
                tail = redactor.flush()
                if tail:
                    yield chunk_frame(tail)
                yield DONE  # always after the flushed tail
                return
            text = redactor.feed(delta_text(data))
            if text:
                yield chunk_frame(text)
    except TimeoutError:
        code = "upstream_timeout"
    except UpstreamProtocolError:
        code = "upstream_protocol_error"
    except httpx.HTTPError:
        code = "upstream_error"
    finally:
        with anyio.CancelScope(shield=True):  # still close the upstream on client disconnect
            await resp.aclose()
    # Failure: the held tail is dropped, not flushed. It may be the head of a PII string
    # whose end never arrived, and an unfinished match cannot be redacted (fail closed).
    log.warning("upstream stream failed: %s (dropped %d held chars)", code, redactor.held)
    yield error_frame(code)


def create_app(client: httpx.AsyncClient, upstream_url: str, stall_s: float = 30.0) -> Starlette:
    async def chat(request: Request):
        raw = await _read_body(request)
        if raw is None:
            return _error(413, "body_too_large")
        try:
            body = ChatRequest.model_validate_json(raw)
        except ValidationError as exc:  # field names and rule types only, never the input
            fields = sorted({".".join(map(str, e["loc"])) or "body" for e in exc.errors()})
            return _error(400, "invalid_request", fields=fields)
        if not body.stream:
            return _error(400, "stream_required")
        upstream = client.build_request("POST", upstream_url, json=body.model_dump(exclude_none=True))
        try:
            resp = await client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            log.warning("upstream connect failed: %s", type(exc).__name__)
            return _error(502, "upstream_unavailable")
        if resp.status_code != 200:  # checked before streaming: after the first byte it is 200
            log.warning("upstream status %d", resp.status_code)
            await resp.aclose()
            return _error(502, "upstream_error")
        return StreamingResponse(
            relay(resp, stall_s), media_type="text/event-stream", headers=SSE_HEADERS,
            background=BackgroundTask(resp.aclose),  # covers a disconnect before the first chunk
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        await client.aclose()

    return Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"])], lifespan=lifespan)


def _default_app() -> Starlette:
    logging.basicConfig(level=logging.INFO)
    url = os.environ.get("UPSTREAM_URL")
    if not url:
        mock = MockUpstream(DEMO, delay=0.05)
        return create_app(httpx.AsyncClient(transport=mock), "http://mock-upstream/v1/chat/completions")
    key = os.environ.get("UPSTREAM_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)
    return create_app(httpx.AsyncClient(headers=headers, timeout=timeout), url)


app = _default_app()
