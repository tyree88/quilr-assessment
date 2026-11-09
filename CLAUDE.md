# CLAUDE.md

Guidance for working in this repository. Each task folder's `README.md` is the reviewer-facing
document; each task folder's `CLAUDE.md` records its task statement, layout and conventions.

## Repository

Four independent take-home tasks, each a small Python program with its own `pyproject.toml`
and `uv.lock`. There is no shared package between them.

| Folder | Task |
|---|---|
| `task1-mcp-server/` | Stdio MCP server with two strictly validated customer-ops tools |
| `task2-mcp-gateway/` | HTTP/JSON-RPC gateway that authorizes `admin_*` tool calls by Bearer-token role |
| `task3-llm-stream-guardrail/` | Streaming LLM proxy that redacts emails, SSNs and card numbers in real time |
| `task4-rate-limit-fallback-router/` | Token-aware sliding-window rate limiter (SQLite) with model failover |

## Shared stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Packages | `uv`, exact pins in `pyproject.toml`, committed `uv.lock` |
| Validation | Pydantic v2, strict models at trust boundaries |
| HTTP server (Tasks 2–4) | Starlette + uvicorn |
| HTTP client | `httpx.AsyncClient` |
| Tests | `pytest` with the anyio plugin (`pytestmark = pytest.mark.anyio`); `httpx.ASGITransport` for in-process apps |
| Lint | `ruff` 0.17.4 |
| Logging | stdlib `logging` to stderr |

## Commands (run inside a task folder)

```bash
uv sync --locked
uv run pytest -q
uvx ruff@0.17.4 check .
```

Tasks 2–4 also ship `probe.py`, a hostile-input run against the app over real sockets. Each task
README gives its run commands. CI (`.github/workflows/ci.yml`) runs install, tests and lint for
every task folder on Python 3.12.

## Conventions

- **Validate at the boundary.** Request bodies, headers and upstream responses are validated
  once where they enter; internal code trusts the validated types.
- **Never leak internals.** Error responses carry fixed messages and codes, never exception text,
  hostnames, stack traces or caller-supplied values.
- **Every non-trivial path has a test.** Guards are mutation-checked: remove the guard, confirm a
  test fails.
- **Mark known shortcuts** in code as `# shortcut: <limit>, <upgrade>` and list them in the
  task README under known limits.
- **No speculative abstraction** and no dependency without a concrete need.
- **Invisible characters in source** are written as `\u` escapes (ruff PLE2515).
- **Git:** stage files by explicit path and check `git diff --cached --stat` before committing.
  `.env`, `PLAN.md` and other local notes are gitignored and never committed.
