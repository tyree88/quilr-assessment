# Task 4 — Rate-Limiting & Model Fallback Router

Guidance for working in this folder. The README is the reviewer-facing document; this file
records the task, the layout, the commands and the conventions the code follows.

## Task statement

> Write a resilient model-routing module for an LLM Gateway that accepts incoming completion requests.
>
> Requirements:
> - Implement a token-aware sliding window rate limiter (e.g., maximum 50,000 tokens/minute per tenant API key).
> - If the primary model endpoint returns a `429 Too Many Requests` status or times out after 3000ms, automatically failover to a secondary backup model provider.
> - Ensure error responses return a standardized gateway error payload without leaking raw upstream stack traces or internal implementation details.
> - Use on disk sqlite for database
>
> Evaluation criteria:
> - Async concurrency handling and timeout race conditions.
> - Accurate rate-limiter state eviction and token tracking logic.
> - Graceful fallback mechanics and standardized error sanitization.

## Layout

| Path | Job |
|---|---|
| `llm_router/config.py` | `Settings`: frozen, strict Pydantic model holding every number |
| `llm_router/schema.sql`, `db.py` | `STRICT` usage table; one short-lived aiosqlite connection per transaction |
| `llm_router/ratelimit.py` | `SlidingWindowLimiter`: `reserve`, `reconcile`, `release` |
| `llm_router/providers.py` | `Provider` protocol, `HttpProvider`, scripted `FakeProvider` |
| `llm_router/router.py` | `ModelRouter`: per-provider deadline and failover rules |
| `llm_router/errors.py` | The closed `GatewayError` contract and `to_response()` |
| `llm_router/app.py` | Starlette app: the trust boundary |
| `llm_router/__main__.py` | `python -m llm_router` entry point |
| `probe.py` | Hostile-input table against the full ASGI app |

## Commands

```bash
uv sync --locked
uv run pytest -q                 # RuntimeWarning is an error
uvx ruff@0.17.4 check .
uv run python probe.py
uv run python -m llm_router      # 127.0.0.1:8000 with fake providers
```

`FAKE_PRIMARY_MODE` (`ok | 429 | 500 | 400 | conn | boom | hang`) scripts the fake primary;
`PRIMARY_URL` / `SECONDARY_URL` select real HTTP providers; `GATEWAY_DB` sets the SQLite file.

## Core design

- **Sliding log:** one row per admitted request `(tenant, ts_ms, tokens)`; the window
  `(now − 60 s, now]` is half-open, in integer milliseconds.
- **Reserve in one transaction:** `BEGIN IMMEDIATE`, evict old rows for all tenants, sum the
  tenant's rows, then insert or deny with `Retry-After`. Each reservation opens its own
  connection, because SQLite's write lock isolates connections, not coroutines sharing one.
- **Tokens:** reserve `ceil(len(prompt) / 4) + max_tokens`, reconcile to the provider's
  `usage.total_tokens`, release the row on failure or cancel (under `asyncio.shield`).
- **Failover:** each provider call runs under `asyncio.timeout(3.0)`. 429, timeout, 5xx and
  connection errors go to the secondary once; other 4xx return `400 invalid_request`; both
  failing returns `503 upstream_unavailable`. A cancel from outside propagates and releases quota.
- **Errors:** `GatewayError` has no message parameter; each type has a fixed public message and
  a `request_id` (also `X-Request-ID`) that matches the server log line holding the real detail.

## Conventions

- **Time is injected** (`now_ms: Callable[[], int]`), wall clock in integer ms, read once per
  reservation, so the window is testable without sleeping.
- **All settings are strict:** zero, negative or unknown fields fail at startup.
- **API keys are stored as `sha256(key)`;** the database file holds no secrets.
- **`httpx.ASGITransport` does not run lifespan,** so tests call `init_db()` themselves.
- **One try/except at the boundary** in `app.py`: a `GatewayError` maps to its payload,
  anything else is logged and returned as `internal`.
