# Task 4 — PLAN

Status: Phase 1 (grill). No code yet.

## Pinned versions (PyPI, 2026-11-08)

Python 3.12 · aiosqlite 0.22.1 · httpx 0.28.1 · starlette 1.10.0 · pydantic 2.14.3 ·
uvicorn 0.54.0 · pytest 9.2.0 · anyio 4.16.0 (pytest plugin, replaces pytest-asyncio) · ruff 0.17.4

## Verified doc facts

- aiosqlite: one `Connection` = one `sqlite3` connection + one worker thread. Every call is queued
  to that thread (source: omnilib/aiosqlite `core.py`, `_connection_worker_thread`).
  → All coroutines that share one connection share **one transaction**. `BEGIN IMMEDIATE` on a
  shared connection does not isolate coroutines; a second `BEGIN` fails with
  "cannot start a transaction within a transaction".
- aiosqlite: a queued function still runs after its awaiting future is cancelled; only the result
  is dropped. → A cancelled coroutine can leave a transaction open on that connection.
- aiosqlite: `isolation_level=None` = autocommit; then we issue `BEGIN IMMEDIATE` ourselves.
- SQLite WAL: `BEGIN IMMEDIATE` takes the write lock at begin; other connections wait up to
  `busy_timeout`, then get `SQLITE_BUSY`. The wait blocks the aiosqlite worker thread, not the loop.
- asyncio 3.12: `asyncio.timeout(3.0)` cancels the current task at the deadline and turns the
  `CancelledError` into `TimeoutError` at block exit. External cancels must propagate.
- httpx: `Timeout(read=…)` is per socket read, not a total deadline. → total 3000 ms needs
  `asyncio.timeout`. Task 2 note: ASGITransport may ignore httpx timeouts → tests use mocks.

## Spec quotes (read 2026-11-08, not from memory)

- RFC 9110 §10.2.3 <https://www.rfc-editor.org/rfc/rfc9110#section-10.2.3>:
  `Retry-After = HTTP-date / delay-seconds`; "A delay-seconds value is a non-negative decimal
  integer". → Ours emits an integer, rounded **up** (never 0 while blocked). Provider value may be
  either form, or garbage, or huge → parse defensively or ignore (decision Q-RA).
- SQLite lang_transaction <https://www.sqlite.org/lang_transaction.html>: "IMMEDIATE causes the
  database connection to start a new write immediately … might fail with SQLITE_BUSY if another
  write transaction is already active on another database connection." "EXCLUSIVE and IMMEDIATE
  are the same in WAL mode." → the lock is **per connection**, confirming trap #1.
- SQLite rescode (517) <https://www.sqlite.org/rescode.html#busy_snapshot>: WAL error when a
  connection "tries to promote a read transaction into a write transaction but finds that another
  database connection has already written". → why deferred `BEGIN` (or option c across
  processes) can fail after passing the check; `busy_timeout` does not help.
- Local check (Python 3.12.3, SQLite 3.45.1): second `BEGIN IMMEDIATE` on the same connection →
  `OperationalError: cannot start a transaction within a transaction`.

## Task 1 retro lessons (applied here)

1. **Probe per unit, not at the end.** `probe.py` (live ASGI app + mock providers, ~25 cases,
   prints PASS/FAIL table) grows with each unit; a unit is done only when its probe rows pass.
   Probe rows: last-token race (N concurrent, exact success count) · primary finishes at
   deadline−ε and deadline+ε · provider 429 with no / `120` / huge / HTTP-date / garbage
   `Retry-After` · both providers fail · client disconnect mid-request (is upstream cancelled?
   reservation released?) · clock at window edge (`ts == now−60000` in or out?) · SQLite busy under
   concurrent writers (two connections, plus a second process).
2. Specs quoted above with links. Provider rate-limit headers: none read, because providers are
   mocks; README says so.
3. **Inherited files:** this worktree has no task 4 code. Nothing inherited except the guide. Guide
   items get questioned too (grill #7 already found wrong).
4. **Edge tests at design time** (all integer ms, never float seconds, in the DB):
   - limit: `used + est == limit` → allow; `== limit + 1` → deny.
   - window: row at `now − 60000` exactly → evicted (half-open window `(now−60s, now]`); at
     `now − 59999` → counts.
   - Retry-After: `ceil((oldest_ts + 60000 − now) / 1000)`, min 1.
   - est larger than the whole limit → deny forever? → reject as `invalid_request` (grill).
   - reconcile actual > est → stored as actual (window may exceed limit briefly; documented).
5. Each decision lands in **PLAN.md + task CLAUDE.md in the same commit**.
6. Git: `git add <explicit paths>`; `git diff --cached --stat` before every commit; no `git rm`
   sweep; in zsh, quote any word starting with `=`.
7. CI: done on main (`.github/workflows/ci.yml`, matrix already lists this folder). It runs
   `uv sync --locked`, `uv run pytest -q`, `uvx ruff@0.17.4 check .` once a `pyproject.toml`
   exists. → commit `uv.lock` with the pyproject; pytest + anyio go in the `dev` dependency group
   (uv syncs it by default); ruff needs no dependency.
8. `requires-python = ">=3.12,<3.13"` in the task pyproject.

## Decisions log

| # | Decision | Choice | Why |
|---|---|---|---|
| 1 | Connection model | **(a) Short-lived aiosqlite connection per `reserve()`**: `BEGIN IMMEDIATE` → evict → sum → insert or `ROLLBACK` → `COMMIT`; `ROLLBACK` + `close()` in `finally`. `# shortcut: connection per reserve, upgrade to asyncio.Queue pool if open cost shows` | SQLite write lock is per connection (lang_transaction), so per-call connections make the DB lock the one guard, holding across uvicorn workers too. Cancel-safe: connection discarded. Rejected (b) shared conn + `asyncio.Lock`: wrong across processes, duplicates the DB lock. Rejected (c) single `INSERT…SELECT`: `SQLITE_BUSY_SNAPSHOT` across processes, hides the transaction. Source: user's default via parent session, 2026-11-08. Task CLAUDE.md grill #7 corrected in the same commit. |
| 2 | Clock | Inject `now_ms: Callable[[], int]`, default `time.time_ns() // 1_000_000`; read **once** per `reserve()`; tests use `FakeClock.advance(ms)` | Wall clock because rows are on disk and shared across processes (monotonic is per-process). Integer ms: no float boundary bugs. Deadline uses the loop's monotonic clock via `asyncio.timeout`, separate concern. Limit: NTP step back → over-count (safe direction), README. |
| 3 | Release on failure | Any non-success exit (both fail, invalid, cancel) → `DELETE WHERE id=?` in `finally` (own short connection, shielded from cancel) | Failed calls don't burn quota. |
| 4 | Budget scope | Per tenant, shared by primary and secondary | Budget is the tenant's, not the provider's. |
| 5 | Fallback on 5xx | Yes, `FALLBACK_ON_5XX=True` config | Brief names 429 + timeout; 5xx is the same "provider can't serve" class. Documented. |
| 6 | Other 4xx from primary | No fallback → `invalid_request` 400, generic message | Secondary won't fix a bad request. |
| 7 | Our `Retry-After` | From oldest row in window: `ceil((oldest_ts + 60000 − now)/1000)`, min 1 | RFC 9110 delay-seconds. Accuracy upgrade (earliest time enough tokens expire) raised at that unit. |
| 8 | Streaming | Out of scope, README | Task 3 covers streaming. |
| 9 | Client disconnect | Cancel → `finally` releases reservation. Whether Starlette 1.10 cancels the handler is a probe row; if not, README known limit | Don't build disconnect watching before the probe says it's needed. |
| 10 | Provider `Retry-After` | Ignored: 429 → immediate fallback; never parsed, never surfaced | Nothing to parse = nothing to break. Probe still sends odd values to prove it. |
| 11 | est > whole limit | `invalid_request` 400 before any DB write | Could never be admitted; 429 + Retry-After would lie. |

Defaults 2–11 taken per user ("Use the recommended defaults"), Phase 2 go from user via parent session, 2026-11-08.

## Open decisions (dependency order)

① connection model (Q1, asked) → ② clock injection → ③ release on failure → ④ secondary shares
budget → ⑤ fallback on 5xx → ⑥ no fallback on other 4xx → ⑦ our Retry-After from oldest row →
⑧ streaming out of scope → **⑨ client disconnect: cancel upstream + release?** (new, from retro) →
**⑩ provider Retry-After: ignore, or skip primary until it expires?** (new) → **⑪ est > limit:
`invalid_request`?** (new).
(CI is settled on main; no decision needed.)

## Rules

- `INTERVIEW_NOTES.md` is local only: gitignored, never committed, never named in an issue or PR.

## Build units (each splits into function-sized units at build time; probe rows land with each)

1. `pyproject.toml` + `uv.lock` + `tests/conftest.py` (anyio backend) — toolchain, CI goes live
2. `config.py` — `Settings` (limit, window, timeouts, db path, fallback_on_5xx)
3. `schema.sql` + `db.py` — `connect(path)`: WAL, `busy_timeout`, `isolation_level=None`, schema + index
4. `errors.py` — `ErrorType` enum, `GatewayError`, error payload model, `to_response`
5. `models.py` — `CompletionRequest` (strict, extra=forbid), `CompletionResponse`, `Usage`
6. `ratelimit.py` `reserve()` — the transaction · window-slide, boundary and **25/25 race** tests
7. `ratelimit.py` `reconcile()` + `release()` — reconcile and release tests
8. `ratelimit.py` Retry-After — on `RateLimited`
9. `providers.py` — `Provider` Protocol, `ProviderResponse`, `ProviderError` kinds, `MockProvider`
10. `router.py` `complete()` — timeout, fallback rules, cancel test, ≈3.0 s timing test
11. sanitize + logging — `request_id`, log real error server-side, body clean test
12. `app.py` — route, `X-API-Key`, handlers, e2e via `ASGITransport`
13. `probe.py` — live-app table (rows grow from unit 6 on)
14. sweeper — bounded-table test
15. `README.md`; `INTERVIEW_NOTES.md` (local only)
