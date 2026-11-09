# Task 4 — Rate-Limiting & Model Fallback Router

Root `CLAUDE.md` rules apply. This file adds task context.
**Format: architectural coding task.** Grader wants to see the *decisions*, not just working code. README carries weight here.

## Task statement (verbatim from assessment)

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

## The four hard problems (name them back to me before any code)

1. **Token counting before the response exists.** You don't know output tokens until the call returns. Options: reserve estimate → reconcile; count prompt only; count after. Rubric says *token-aware* and *accurate tracking* → **reserve-then-reconcile**.
2. **Sliding window on SQLite under concurrency.** Check-then-insert is a race. Must be one transaction with `BEGIN IMMEDIATE`, or a single `INSERT ... WHERE (SELECT SUM(...)) + :n <= :limit` statement.
3. **Timeout race.** `asyncio.wait_for(primary, 3.0)` → if it times out, cancel the primary (don't leak the task), then call secondary. If primary returns 429, same path. Secondary also gets a timeout. Both fail → standardized error.
4. **Sanitization.** Upstream errors carry hostnames, stack traces, provider error JSON. Map everything to one `GatewayError` shape. Log the real thing server-side with a correlation id; return the id to the client.

## Design decisions (settled unless rubric says otherwise)

| Decision | Choice | Why |
|---|---|---|
| Window type | **Sliding log** (one row per request, `tenant, ts_ms, tokens`), sum over `[now−60s, now]` | Rubric literally says *sliding window* and *state eviction*. Fixed-window and token-bucket are different algorithms; don't drift. |
| Storage | `aiosqlite`, file on disk (`./gateway.db`), `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000` | Rubric mandates on-disk SQLite. WAL = concurrent readers + one writer without `database is locked` spam. |
| Atomic check+reserve | Single `BEGIN IMMEDIATE` transaction: `SELECT COALESCE(SUM(tokens),0) ... WHERE ts_ms > ?` → compare → `INSERT` → `COMMIT` | `BEGIN IMMEDIATE` takes the write lock up front → no two coroutines can both pass the check |
| Token estimate | `len(prompt) // 4 + max_tokens` (ponytail) | Reserve pessimistically; reconcile with `usage.total_tokens` from the provider response via `UPDATE ... SET tokens=? WHERE id=?`. `# ponytail: ceiling=chars/4, upgrade=tiktoken` |
| Eviction | `DELETE FROM usage WHERE ts_ms < now−60s` — run **inside the same transaction** before the SUM, plus a periodic sweeper | Rubric: *accurate state eviction*. Sweep-in-tx keeps the table bounded without a background task; background sweeper is bonus. |
| Timeout | `asyncio.timeout(3.0)` (3.12) or `asyncio.wait_for` around the **whole** primary call, including connect | 3000 ms is the brief's number; config, not magic |
| Fallback trigger | primary `429` **or** timeout **or** connection error (**grill**: 5xx too?) | Brief names 429 + timeout. 5xx is a judgment call → make it configurable, default on, document. |
| Providers | `Protocol` with `async def complete(req) -> ProviderResponse`; two **mock** providers with scripted behavior (`ok`, `429`, `sleep(5)`, `raise`) | Deterministic tests; no API keys for the grader |
| Error payload | `{"error": {"type": "...", "message": "...", "request_id": "..."}}` with a closed `type` enum: `rate_limited`, `upstream_unavailable`, `upstream_timeout`, `invalid_request`, `internal` | One shape, one place (`errors.py`), nothing else ever reaches the client |

## Docs to pull on entry

1. `aiosqlite` — connection per app vs per request, `execute`, `commit`, `isolation_level=None` + manual `BEGIN IMMEDIATE`
2. SQLite: `BEGIN IMMEDIATE` semantics, WAL mode, `busy_timeout`, index on `(tenant, ts_ms)`
3. `asyncio.timeout()` / `wait_for` — cancellation semantics; what happens to the inner task; `CancelledError` must propagate
4. `httpx` — `Timeout(connect=, read=, write=, pool=)`, status handling, why `raise_for_status` text must never reach the client
5. Starlette exception handlers — one `GatewayError` handler, one catch-all → `internal` with `request_id`

## Proposed module layout (names only)

```
task4-rate-limit-fallback-router/
├── pyproject.toml                 starlette · uvicorn · httpx · aiosqlite · pydantic · pytest · anyio plugin (no pytest-asyncio)
├── src/llm_router/
│   ├── db.py                      open_db(path) -> aiosqlite.Connection (WAL, busy_timeout, schema, index)
│   ├── schema.sql                 CREATE TABLE usage(id INTEGER PK, tenant TEXT, ts_ms INTEGER, tokens INTEGER); INDEX (tenant, ts_ms)
│   ├── ratelimit.py               class SlidingWindowLimiter: reserve(tenant, est_tokens) -> ReservationId | raise RateLimited · reconcile(id, actual) · evict(now) · window_usage(tenant)
│   ├── providers.py               Protocol ProviderClient · MockProvider(script) · HttpProvider(base_url, key)
│   ├── router.py                  class ModelRouter: complete(req) → try primary w/ timeout → on 429/timeout/conn-err → secondary w/ timeout → raise UpstreamUnavailable
│   ├── errors.py                  GatewayError(type, status, message) + enum + to_response(request_id); sanitize(exc) -> GatewayError
│   ├── models.py                  CompletionRequest (Pydantic, extra=forbid) · CompletionResponse
│   ├── app.py                     POST /v1/completions · header X-API-Key → tenant · request_id middleware · exception handlers
│   └── config.py                  Settings: limit_tokens=50_000, window_ms=60_000, upstream_timeout_s=3.0 (each provider), busy_timeout_ms, db_path
└── tests/
    ├── test_ratelimit_window.py   fill to 49k → allow 1k → deny 1 → advance clock 61s → allow; eviction row count
    ├── test_ratelimit_concurrency.py  50 coroutines × 2k tokens, limit 50k → exactly 25 pass (asyncio.gather) — THE race test
    ├── test_reconcile.py          reserve 1000, reconcile 300 → window shows 300
    ├── test_router_fallback.py    primary 429 → secondary used; primary sleep(5) → timeout at ~3.0s → secondary; both fail → upstream_unavailable; primary ok → secondary never called
    ├── test_timeout_cancel.py     after timeout, primary coroutine is cancelled (no "Task was destroyed" warnings; mock records cancellation)
    ├── test_sanitize.py           provider raises with hostname + traceback → response body contains neither; request_id present; server log contains both
    └── test_e2e.py                ASGITransport; 429 from gateway has Retry-After; payload shape validated by Pydantic model
```

## Grill checklist (ask me these, one at a time, with your pick)

1. Clock source: `time.time()` or injectable `Clock` protocol? → **Injectable.** Window tests need to advance time without `sleep`.
2. Reservation on failure: if both providers fail, do we **release** the reservation? → Yes, `DELETE WHERE id=?` in a `finally`. Interview line: "failed calls don't burn quota."
3. Does the secondary provider share the tenant's budget? → Yes. Budget is per tenant, not per provider.
4. Fallback on 5xx? → **Decided: always** (fixed, not configurable). 429, timeout, 5xx and connection errors all mean "this provider cannot serve now".
5. Fallback on primary **4xx other than 429** (400 bad request)? → **No.** Request is wrong; secondary won't fix it. Return `invalid_request`.
6. `Retry-After` on our own 429 → compute from oldest row in window: `(oldest_ts + 60s − now)`. Cheap, correct, impressive.
7. One `aiosqlite` connection shared, or pool? → **Decided: short-lived connection per `reserve()`** + `BEGIN IMMEDIATE` + `busy_timeout`, no app-level lock. A shared aiosqlite connection is one `sqlite3` connection = one transaction for every coroutine, so `BEGIN IMMEDIATE` on it isolates nothing (second `BEGIN` → "cannot start a transaction within a transaction"). SQLite's write lock is per connection, so per-call connections make it the one guard, across processes too. See `README.md` → Design and Decisions.
8. Streaming responses? → **Out of scope.** Say so in README; Task 3 covers streaming.

## Reserve-then-reconcile transaction (pair-build first; it is the heart of the grade)

```sql
BEGIN IMMEDIATE;
DELETE FROM usage WHERE tenant = :t AND ts_ms < :now_ms - 60000;          -- evict in-tx
SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant = :t;             -- window sum
-- in Python: if used + :est > LIMIT → ROLLBACK; raise RateLimited(retry_after)
INSERT INTO usage(tenant, ts_ms, tokens) VALUES (:t, :now_ms, :est);      -- reserve
COMMIT;
-- later:
UPDATE usage SET tokens = :actual WHERE id = :id;                          -- reconcile
```

Grill: *why `BEGIN IMMEDIATE` and not `BEGIN`?* → deferred `BEGIN` takes a read lock first; two coroutines both read 49k, both try to write → one gets `SQLITE_BUSY` after passing the check, or worse, with `busy_timeout` both commit → **over-admit**. `IMMEDIATE` serializes the check.

## Pressure Test Checklist

- [ ] **Concurrency race**: 50 concurrent requests × 2,000 est tokens, limit 50,000 → exactly 25 admitted, 25 rejected. Not 26. Not 24.
- [ ] Window slides: request at t=0 (30k), t=30s (20k), t=59s (1 token) → denied; t=61s (1 token) → allowed; row from t=0 evicted
- [ ] Reconcile: reserve 5,000, actual 1,200 → next request sees 1,200 used
- [ ] Failed call releases reservation
- [ ] Primary 429 → secondary called once, primary not retried
- [ ] Primary hangs → secondary called at **≈3.0s**, assert `2.9 < elapsed < 3.3`
- [ ] Primary cancelled on timeout: mock records `CancelledError`; `-W error::RuntimeWarning` run is clean
- [ ] Both fail → `{"error":{"type":"upstream_unavailable","request_id":"..."}}`, HTTP 503
- [ ] Our rate limit → HTTP 429 + `Retry-After` header + `type: rate_limited`
- [ ] Provider exception message with `10.0.0.5` and `Traceback` → **absent** from body, present in server log with same `request_id`
- [ ] Unknown exception anywhere → `type: internal`, no message detail
- [ ] DB file exists on disk after run; `PRAGMA journal_mode` returns `wal`
- [ ] Table bounded: after 10k requests over 10 min with sweeper, row count ≈ one window's worth
- [ ] Missing `X-API-Key` → 401 `invalid_request`
- [ ] `ruff` clean, `pytest -q` green, README has: algorithm choice, transaction explanation, error table, config table

## Interview questions you will drill me on

- "Why sliding log over sliding window counter or token bucket? Cost?" (exact, O(rows per window); upgrade: two-bucket approximation if row volume hurts)
- "Show me the race and show me the line that closes it."
- "How do you count tokens you haven't generated yet?"
- "What happens to the primary coroutine when the timeout fires?"
- "Why does the client get a `request_id` and not the error?"
- "Where would this break at 1,000 RPS?" (single SQLite writer → Redis `ZADD`/`ZREMRANGEBYSCORE` + Lua; say it)
- "What does a 429 from *your* gateway look like vs a 429 from the *provider*?" (ours: `rate_limited` + Retry-After; theirs: triggers fallback, never surfaces)
