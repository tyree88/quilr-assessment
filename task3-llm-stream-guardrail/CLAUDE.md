# Task 3 — LLM Gateway Streaming Guardrail (PII redaction)

Guidance for working in this folder. The README is the reviewer-facing document; this file
records the task, the layout, the commands and the conventions the code follows.

## Task statement

> Implement an LLM Gateway proxy endpoint that routes text generation requests to an LLM provider and streams the response back to the client.
>
> Requirements:
> - Intercept the returning response chunk stream in real time.
> - Parse incoming stream deltas to detect and redact sensitive patterns (such as Emails, SSNs, or credit card numbers), replacing them with `[REDACTED]`.
> - Ensure the stream stays responsive without accumulating the full response in memory, minimizing Time To First Token (TTFT) and overall stream latency.
>
> Evaluation criteria:
> - Efficient asynchronous stream chunking and buffer state management.
> - Performant string/regex pattern matching over partial text streams.
> - Memory efficiency and low-latency proxying design.

## Layout

| Path | Job |
|---|---|
| `guardrail/patterns.py` | `EMAIL`, `SSN`, `CARD`, `luhn_ok`, the combined `PII_RE`, the reference `redact()` |
| `guardrail/redactor.py` | `StreamRedactor.feed()` / `flush()`: the bounded carry buffer |
| `guardrail/sse.py` | SSE line parser, `delta_text`, `chunk_frame`, `error_frame` |
| `guardrail/app.py` | Starlette `POST /v1/chat/completions`: request validation, upstream status check, relay |
| `guardrail/provider.py` | Mock upstream on a real `httpx` transport (scripted chunks, delays, failures) |
| `tests/` | Patterns, redactor, SSE, provider, app, and stream behaviour (TTFT, memory, disconnect) |
| `probe.py` | Hostile-input run over a live uvicorn socket |

## Commands

```bash
uv sync --locked
uv run pytest -q
uvx ruff@0.17.4 check .
uv run python probe.py
uv run uvicorn guardrail.app:app    # :8000 with the built-in mock upstream
```

`UPSTREAM_URL` (and optionally `UPSTREAM_API_KEY`) proxies a real OpenAI-compatible endpoint.

## Core design

PII can span chunks, so a per-chunk regex leaks, and holding the whole response destroys TTFT.
`StreamRedactor` scans its buffer once per `feed()`, emits every match that can no longer grow
as `[REDACTED]`, emits all text before the hold point, and holds only the trailing run that could
still become PII (at most `K = 128` characters, the longest possible match). The buffer holds raw
text only, so `[REDACTED]` is never re-matched. `flush()` runs at stream end, before `[DONE]`.

Invariant, tested for many chunkings and by fuzzing:
`"".join(feed(c) for c in chunks) + flush() == redact(s)`.

## Conventions

- **Every pattern quantifier is bounded,** so the maximum match length (and so `K`) is provable.
  Changing a pattern means re-deriving `K`; `StreamRedactor` refuses a `k` below `MAX_MATCH`.
- **The hot path never accumulates the stream** (no list of chunks); memory is bounded by `K`.
- **Fail closed:** on a mid-stream upstream failure the held tail is dropped, not flushed, and
  the client gets a final `event: error` frame with a fixed code.
- **No upstream text reaches the client;** request errors name fields, never input values.
- **`httpx.ASGITransport` buffers the whole response,** so TTFT, disconnect and memory are
  tested by calling the ASGI app directly (`tests/test_stream_behaviour.py`) and over real
  sockets in `probe.py`.
- **The mock upstream yields to the event loop on every step,** like a socket does.
