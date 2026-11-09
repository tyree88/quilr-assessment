# Task 4: Rate-limiting and model fallback router

A model-routing module for an LLM gateway:

- a token-aware **sliding-window** limiter (50,000 tokens per minute per tenant API key) stored in on-disk SQLite;
- automatic failover to a secondary provider when the primary returns `429` or takes longer than **3000 ms**;
- one standardized error payload that never carries upstream text.

## Run

```bash
cd task4-rate-limit-fallback-router
uv sync
uv run pytest -q                      # 72 tests, RuntimeWarning is an error
uvx ruff@0.17.4 check .
uv run python probe.py                # 41 hostile-input rows against the full ASGI app
uv run python -m llm_router           # gateway on 127.0.0.1:8000 with fake providers
```

Try the fallback locally:

```bash
FAKE_PRIMARY_MODE=hang uv run python -m llm_router
```

```bash
curl -s localhost:8000/v1/completions -H 'X-API-Key: demo' -d '{"prompt":"hi","max_tokens":16}'
```

`FAKE_PRIMARY_MODE` takes `ok | 429 | 500 | 400 | conn | boom | hang`. Set `PRIMARY_URL` and `SECONDARY_URL` to use real HTTP providers. Logs go to stderr.

## Layout

| File | Role |
|---|---|
| `llm_router/config.py` | `Settings`: frozen, strict Pydantic model holding every number |
| `llm_router/schema.sql`, `db.py` | `STRICT` usage table; one short-lived aiosqlite connection per transaction |
| `llm_router/ratelimit.py` | `SlidingWindowLimiter`: `reserve`, `reconcile`, `release` |
| `llm_router/providers.py` | `Provider` protocol, `HttpProvider`, scripted `FakeProvider` |
| `llm_router/router.py` | `ModelRouter`: per-provider deadline and the failover rules |
| `llm_router/errors.py` | the closed error contract and `to_response()` |
| `llm_router/app.py` | Starlette app: the trust boundary |
| `probe.py` | hostile-input table |

## Design

### Algorithm: a sliding log

There is one row per admitted request: `(tenant, ts_ms, tokens)`. A request is admitted when the sum of the tenant's rows in the window `(now − 60 s, now]` plus its estimate is at most the limit.

- **Why a sliding log:** it is exact. A fixed window lets a tenant spend 2× the limit across a window edge. A token bucket and a two-bucket "sliding window counter" are approximations, and the rubric asks for *accurate* eviction and tracking.
- **The cost:** storage is O(requests in one window). See *Known limits* for when that matters.
- **The window is half-open.** A row exactly 60,000 ms old is out; one 59,999 ms old still counts. All times are **integer milliseconds**, and the `STRICT` table rejects a float.

### The reserve transaction (`ratelimit.py`)

```sql
BEGIN IMMEDIATE;                                            -- take the write lock first
DELETE FROM usage WHERE ts_ms <= :now - 60000;              -- evict, all tenants
SELECT COALESCE(SUM(tokens),0), MIN(ts_ms) FROM usage WHERE tenant = :t;
-- used + est > limit  -> ROLLBACK, 429 with Retry-After
INSERT INTO usage (tenant, ts_ms, tokens) VALUES (:t, :now, :est);
COMMIT;
```

**The race this closes:** check-then-insert done in two steps lets two requests both read 49,000 and both insert. `BEGIN IMMEDIATE` takes SQLite's write lock **before** the read, so a second connection waits in `BEGIN` (up to `busy_timeout`) and sums only after the first has committed. The test: 50 concurrent reservations of 2,000 against 50,000 admit **exactly 25**, both in the unit tests and through the full app in the probe.

**One connection per transaction.** An aiosqlite `Connection` is one `sqlite3` connection driven by one worker thread. If coroutines shared it, they would share one transaction, and `BEGIN IMMEDIATE` would isolate nothing: a second `BEGIN` on the same connection fails with *"cannot start a transaction within a transaction"*. SQLite's lock works between connections ([lang_transaction](https://www.sqlite.org/lang_transaction.html)), so each reservation opens its own connection. That makes the database lock the only guard, and it still holds with several uvicorn workers.

**What the mutation tests showed.** Replacing `IMMEDIATE` with a plain `BEGIN` still passes, because the first statement is a `DELETE` and SQLite takes the write lock on the first write. Moving the read before the write under a plain `BEGIN` breaks the race test. `IMMEDIATE` keeps the guard independent of statement order.

**Any exit path rolls back** (deny, error, or cancel), so the lock is released at once.

**Eviction covers every tenant.** It uses an index on `ts_ms`, so idle tenants cannot grow the table, and no background sweeper is needed. The probe sends 200 requests from 50 tenants over 10 simulated minutes and ends with ≤ one window of rows.

### Counting tokens before the response exists

- **Reserve the worst case:** `ceil(len(prompt) / 4) + max_tokens`.
- **Reconcile after the call:** a single `UPDATE` replaces the estimate with the provider's `usage.total_tokens`.
- **Release on failure or cancel:** `DELETE` the row, run under `asyncio.shield`, so failed calls don't burn quota.
- **Too big to ever fit:** an estimate larger than the whole limit is `400 invalid_request`, not a `429` whose `Retry-After` would never come true.

### Our `Retry-After`

It is `ceil((oldest_ts + 60 s − now) / 1000)`: the moment the tenant's oldest row leaves the window, as RFC 9110 [delay-seconds](https://www.rfc-editor.org/rfc/rfc9110#section-10.2.3). Eviction has just removed every row ≥ 60 s old, so the value is always ≥ 1.

### Fallback (`router.py`)

Each provider call runs under `asyncio.timeout(3.0)`. The deadline covers connecting and the full response; httpx's own `read` timeout restarts on every read, so it can't enforce a total deadline.

- **On expiry:** asyncio cancels the awaited call **in place**. It runs in the same task, so nothing is left running. The test asserts the provider recorded the `CancelledError` and that no task outlived it.
- **A cancel from outside** (the client went away) is not converted into a timeout. It propagates, and the reservation is released.

| Primary outcome | Router does |
|---|---|
| success | return it; the secondary is never called |
| `429`, timeout, `5xx`, connection error | call the secondary once; the primary is not retried |
| other `4xx` | `400 invalid_request` (the secondary would reject the same request) |
| any other exception | propagates to the app → `500 internal` |
| secondary also fails | `503 upstream_unavailable` |

The fallback rule is **fixed, not configurable**: 429, timeout, 5xx and connection errors all mean "this provider cannot serve now". The provider's own `Retry-After` is **not parsed**. A 429 fails over immediately and never reaches the client, so there is nothing to mis-parse. The probe sends none, `120`, an HTTP-date, 40 digits and garbage.

The secondary shares the tenant's budget, because the budget belongs to the tenant, not the provider.

### Errors (`errors.py`)

`GatewayError` has **no message parameter**. The client sees a fixed public message per type, so provider text, hostnames and tracebacks have no path into a response body. `app.py` has one try/except at the boundary: a `GatewayError` maps to its payload, and anything else is logged with `log.exception` and returned as `internal`.

```json
{"error": {"type": "rate_limited", "message": "Token rate limit exceeded for this API key.", "request_id": "3f2a…"}}
```

The `request_id` is also sent as the `X-Request-ID` header. The server log line carries the same id together with the real provider status and detail, so support can join the two.

| `type` | HTTP | When |
|---|---|---|
| `rate_limited` | 429 + `Retry-After` | the tenant's window is full |
| `upstream_unavailable` | 503 | both providers failed (429, timeout, 5xx or connection error) |
| `invalid_request` | 400 | bad JSON, unknown field, wrong type, primary 4xx, estimate over the limit |
| `invalid_request` | 401 | missing or blank `X-API-Key` |
| `invalid_request` | 404 / 405 | unknown path or method |
| `internal` | 500 | anything unexpected, including a SQLite lock held past `busy_timeout` |

API keys are stored as `sha256(key)`, so the database file holds no secrets.

## Configuration (`Settings`)

| Field | Default | Meaning |
|---|---|---|
| `limit_tokens` | 50,000 | tokens per tenant per window |
| `window_ms` | 60,000 | window length, integer ms |
| `upstream_timeout_s` | 3.0 | deadline for **each** provider call |
| `busy_timeout_ms` | 5,000 | how long a reservation waits for another connection's write lock |
| `db_path` | `gateway.db` | on-disk SQLite file, WAL mode (`GATEWAY_DB` for `python -m llm_router`) |

All fields are strict: a zero, a negative or an unknown field fails at startup.

## Verification

- **`uv run pytest -q`: 72 passed.** Covered: the window slide (0 s / 30 s / 59 s / 61 s); the exact limit and limit+1; the half-open edge; `Retry-After` rounding; idle-tenant eviction; the **25/25 race**; reconcile and release; failover per status; the 2.9–3.3 s timing with the primary cancelled and no task left over; outside cancel; an end-to-end leak check (hostname and traceback absent from the body, present in the log under the same `request_id`); client cancel releasing quota.
- **`uv run python probe.py`: 41/41 PASS.** It also runs a second **process** that holds the SQLite write lock, both within and past `busy_timeout`.
- **Mutation checks:** every guard was broken once to watch its test fail. That includes STRICT, WAL, eviction `<=`, the limit comparison, per-tenant-only eviction, no-op reconcile/release, each failover status, removing the timeout, swallowing an outside cancel, the release on cancel, the catch-all, the blank-key check and the 404 handler.

## Known limits

- **One SQLite writer.** Every reservation takes the database write lock, so throughput is bounded by sequential write transactions on one file (not benchmarked here). Past that, move the log to Redis: `ZADD` plus `ZREMRANGEBYSCORE` in one Lua script per tenant gives the same exact window with no single-file lock.
- **The token estimate is `chars/4`.** Upgrade to the provider's tokenizer. Reconcile corrects the stored value either way.
- **Reconcile can push a tenant over** if the provider uses more than the estimate. Every request reserves its full `max_tokens`, so this needs a provider that ignores `max_tokens`.
- **`Retry-After` uses the oldest row.** If that row frees fewer tokens than the next request needs, the client waits again. Upgrade: walk the rows oldest-first until enough expire.
- **Wall-clock time.** Rows are shared across processes and restarts, so time is `time.time_ns()`, not a monotonic clock. An NTP step backward briefly over-counts, which is the safe direction.
- **Client disconnect behind uvicorn.** The tests and the probe prove the cancel path in-process: the request is cancelled, the primary is cancelled and the quota is released. Whether uvicorn cancels a handler when a client disconnects was not measured here.
- **A lock held past `busy_timeout`** returns `500 internal`. A dedicated `503` with `Retry-After` would be friendlier.
- **Out of scope:** streaming responses (Task 3), per-tenant limits, schema migrations (`CREATE … IF NOT EXISTS` only), and `PRAGMA synchronous=NORMAL` (faster, but can lose the last commits on power loss, which would under-count usage).
- The lifespan hook creates the schema. `httpx.ASGITransport` doesn't run lifespan, so the tests call `init_db()` themselves.

## Decisions

1. **A connection per reservation, with `BEGIN IMMEDIATE`.** A shared connection was rejected because it shares one transaction. An `asyncio.Lock` was rejected because it is wrong across processes. A single `INSERT … SELECT` was rejected because of `SQLITE_BUSY_SNAPSHOT` across processes.
2. **Injected clock** `now_ms: Callable[[], int]`, wall time in integer ms, read once per reservation.
3. **Release on any failure or cancel.**
4. **The secondary shares the tenant budget.**
5. **Fail over on 429, timeout, 5xx and connection errors (fixed); no failover on other 4xx.**
6. **`Retry-After` from the oldest row, integer seconds, rounded up.**
7. **Ignore the provider's `Retry-After`.**
8. **An estimate over the limit is `invalid_request`.**
9. **One deadline setting for both providers.**
