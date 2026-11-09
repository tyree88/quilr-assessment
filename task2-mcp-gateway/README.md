# Task 2 — MCP Security Gateway

An HTTP reverse proxy that sits between an AI agent and a downstream MCP server.
It reads the caller's role from a Bearer token and blocks `admin_*` tool calls for non-admins
**before** anything reaches the downstream server.

## Request flow

```
client ──POST /mcp──▶ gateway ──▶ downstream MCP server
                        │
   1. Bearer token → role         (none/unknown → 401)
   2. Parse JSON-RPC envelope     (-32700 / -32600)
   3. Header/body consistency     (Mcp-Name mismatch → 400 + -32020)
   4. Authorize tools/call        (viewer + admin_* → -32001, downstream untouched)
   5. Forward, sanitize failures, pass the response through
```

## Target spec

MCP **2026-07-28** (Streamable HTTP), compatible with 2025-11-25 clients.
`-32001 Unauthorized Tool Call` is required by the task brief. MCP 2026-07-28 marks
`-32000..-32019` as legacy, and we document that tension rather than hide it.

## Error codes

| Situation | HTTP | Code | Message |
|---|---|---|---|
| Missing/invalid Bearer token | 401 + `WWW-Authenticate: Bearer` | `-32001` (`id: null`) | Unauthorized |
| Body not JSON | 200 | `-32700` | Parse error |
| Not a JSON-RPC 2.0 request / batch | 200 | `-32600` | Invalid Request |
| `tools/call` with `params.name` missing, not a string, or containing invisible (Cf) characters | 200 | `-32602` | Invalid params |
| `Mcp-Name` header ≠ `params.name` | 400 | `-32020` | Header mismatch |
| Viewer calls `admin_*` | 200 | `-32001` | Unauthorized Tool Call |
| Viewer sends `admin_*` as a notification (no `id`) | 403 | — | no body (spec: no response to notifications) |
| Downstream unreachable | 502 | `-32603` | Upstream unavailable (`data.reason: unreachable`) |
| Downstream timeout | 504 | `-32603` | Upstream unavailable (`data.reason: timeout`) |
| Downstream 5xx / not JSON-RPC | 502 | `-32603` | Upstream unavailable (`data.reason: bad_response`) |
| Downstream JSON-RPC error | passed through | theirs | byte-identical |

## Upgrades (deliberately not built)

- **Hide `admin_*` from viewers in `tools/list`.** Least privilege says yes. Not built because the
  brief requires transparent forwarding. The `tools/call` deny is the real control: a viewer who
  knows the name still gets `-32001`.
- **Allow-list per role instead of an `admin_` deny-prefix.** The prefix check normalizes (NFKC +
  casefold) and rejects invisible characters, but it can't catch confusables such as Cyrillic `а`.
  A per-role allow-list closes that class of bypass entirely.

## Run (once built)

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run uvicorn mcp_gateway.app:app
```
