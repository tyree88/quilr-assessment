# Task 2 — MCP Security Gateway

Guidance for working in this folder. The README is the reviewer-facing document; this file
records the task, the layout, the commands and the conventions the code follows.

## Task statement

> Build a lightweight HTTP/JSON-RPC reverse proxy (in Node.js or Python) that acts as an MCP Gateway sitting between an AI agent client and a downstream mock MCP server.
>
> Requirements:
> - Read an incoming `Bearer <token>` HTTP header and extract the user's role (e.g., `admin` or `viewer`).
> - Inspect the incoming MCP JSON-RPC payload:
>   - If method is `tools/list`, forward the request transparently to the downstream server.
>   - If method is `tools/call`, inspect `params.name`. If `params.name` starts with `admin_` (e.g., `admin_reset_key`), verify that the token role is `admin`. If not authorized, intercept and return a JSON-RPC Error (`-32001: Unauthorized Tool Call`) directly without calling the downstream server.
>
> Evaluation criteria:
> - Parsing JSON-RPC wire format structures correctly.
> - Proxy middleware construction and HTTP request/response forwarding.
> - Fine-grained, method-level authorization logic and clean error handling.

## Layout

| Path | Job |
|---|---|
| `mcp_gateway/app.py` | `create_app()` and the `app_from_env()` uvicorn factory; the one `POST /mcp` route |
| `mcp_gateway/auth.py` | `extract_bearer` (RFC 6750 grammar), `resolve_role` (static token map) |
| `mcp_gateway/jsonrpc.py` | `JsonRpcRequest` (strict Pydantic envelope), `parse_request(bytes)` |
| `mcp_gateway/policy.py` | `authorize(role, request)` → `allow` / `deny` / `invalid_params` |
| `mcp_gateway/forward.py` | `forward()`: header allowlist, deadline, pass-through or sanitized error |
| `mcp_gateway/errors.py` | Error-code constants and `make_error()`, the only error builder |
| `mock_downstream/app.py` | Mock MCP server with `calls` / `completed` counters; tests and probe only |
| `tests/` | Unit tests per module plus `test_app.py`, end to end over `httpx.ASGITransport` |
| `probe.py` | Hostile-input probe against the gateway and mock under real uvicorn |

## Commands

```bash
uv sync --locked
uv run pytest -q
uvx ruff@0.17.4 check .          # same version CI runs
uv run python probe.py           # needs local port binding
```

Run the servers: see the README.

## Pipeline

`POST /mcp` runs, in this order: authenticate → parse → authorize → forward.
Authentication comes first so anonymous callers learn nothing about the parser; authorization
comes before any network I/O so a denied call never reaches the downstream.

## Error codes

| Situation | HTTP | Code |
|---|---|---|
| Missing / malformed / unknown Bearer token | 401 + `WWW-Authenticate: Bearer` | `-32001` "Unauthorized", `id: null` |
| Body not JSON | 200 | `-32700` |
| Not one valid JSON-RPC request (batch, scalar, repeated key, bad envelope) | 200 | `-32600` |
| `tools/call` name missing, not a string, or holding a Cf character | 200 | `-32602` |
| Non-admin calls `admin_*` | 200 | `-32001` "Unauthorized Tool Call" |
| Denied or invalid notification | 403 / 400 | no body |
| Downstream unreachable / timeout / non-JSON-RPC answer | 502 / 504 / 502 | `-32603`, `data.reason` |
| Downstream JSON-RPC answer | passed through | unchanged |

## Conventions

- **Target spec:** MCP 2026-07-28. `-32001` comes from the task statement even though MCP marks
  `-32000..-32019` as legacy; do not add other codes from that range.
- **Validation lives at the boundaries:** `parse_request` for the body, `extract_bearer` for the
  header, `forward` for the upstream answer. Keep it there rather than adding checks elsewhere.
- **The `admin_` rule lives only in `policy.py`** (NFKC + casefold, Cf characters rejected).
  Never add a second `startswith("admin_")` anywhere.
- **No caller or upstream text in errors.** Messages are constants; `data.reason` is a fixed word.
- **Never forward `Authorization` downstream.** Headers go through the allowlist in `forward.py`.
- **Deadlines use `anyio.fail_after`.** `httpx.ASGITransport` does not enforce httpx timeouts.
- **Tests:** the anyio pytest plugin (`pytestmark = pytest.mark.anyio`), no pytest-asyncio.
  `ASGITransport` does not run lifespan, so tests enter `app.router.lifespan_context(app)`.
  Write invisible characters in source as `\u` escapes.
- **Dependencies:** exact pins in `pyproject.toml` and a committed `uv.lock`; CI runs
  `uv sync --locked` on Python 3.12.
- **Shortcuts** are marked in the code with `# shortcut: <limit>, <upgrade>` and listed in the
  README under Known limits.
