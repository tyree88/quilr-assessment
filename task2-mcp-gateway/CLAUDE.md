# Task 2 — MCP Security Gateway Proxy (tool filtering + auth)

Root `CLAUDE.md` rules apply. This file adds task context.
**Format: take-home.** More room for tests and a README than Task 1 — use it.

## Task statement (verbatim from assessment)

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

## Design decisions (settled unless rubric says otherwise)

| Decision | Choice | Why |
|---|---|---|
| Framework | Starlette raw `Request`/`Response`, no FastAPI | Rubric scores *parsing JSON-RPC wire format*. FastAPI's body models would hide that work. We parse the envelope ourselves with Pydantic. |
| Token → role | Static map in `tokens.py` (`{"tok_admin_...": "admin", "tok_viewer_...": "viewer"}`) loaded from env/JSON | Rubric says *extract the role*, not *implement OAuth*. Mark `# ponytail: ceiling=static map, upgrade=JWT verify w/ JWKS`. |
| Downstream | Our own mock: tiny Starlette app answering `tools/list` + `tools/call` over Streamable HTTP-shaped JSON-RPC | Self-contained, testable in-process, reviewer runs one command |
| Forwarding | `httpx.AsyncClient` with explicit timeout, pass through status code + body + `Content-Type`; strip hop-by-hop headers | Rubric: *HTTP request/response forwarding* |
| Batch requests | Reject with `-32600` unless we deliberately support them | MCP spec removed JSON-RPC batching (2025-06-18+). One line in README. |
| Unknown method | Forward transparently | Gateway is allow-by-default on methods, deny-by-rule on `tools/call` names |

## Docs to pull on entry

1. **JSON-RPC 2.0 spec** — request object, error object, reserved codes `-32700..-32603`, server-defined range `-32000..-32099`
2. **MCP spec**: `tools/list`, `tools/call` params shape (`name`, `arguments`), Streamable HTTP transport (POST JSON body, `Mcp-Session-Id`, `Accept`)
3. **MCP authorization spec** — how Bearer tokens are expected on HTTP transport; what a 401 vs a JSON-RPC error means
4. `httpx` AsyncClient: `timeout`, `app=`/`ASGITransport` for tests, header forwarding
5. Starlette: `Request.body()`, `Response(content, status_code, media_type)`

## Error code map (we own this — put it in `errors.py` and the README)

| Situation | Transport | Code | Message |
|---|---|---|---|
| Missing/malformed `Authorization` header | HTTP **401** + JSON-RPC error body | `-32001` | `Unauthorized Tool Call` *or* `Unauthorized` — **grill this** |
| Unknown token | HTTP 401 | `-32001` | |
| Body not JSON | HTTP 200 (JSON-RPC convention) | `-32700` | `Parse error` |
| Not a JSON-RPC 2.0 request object | 200 | `-32600` | `Invalid Request` |
| `tools/call` missing `params.name` | 200 | `-32602` | `Invalid params` |
| Viewer calls `admin_*` | 200 | **`-32001`** | **`Unauthorized Tool Call`** (rubric literal) |
| Downstream unreachable / timeout | 200 | `-32000` or `-32603` | `Upstream unavailable` — **never** the httpx message |

## The design tensions you must grill me on (in this order)

1. **No/invalid Bearer token: 401 only, or 401 + JSON-RPC error body?**
   Recommended: **both**. HTTP 401 for HTTP-native clients, JSON-RPC `-32001` body for MCP clients. Rubric line: *clean error handling*.
2. **Does `tools/list` get filtered too?** Rubric says forward *transparently*. Recommended: **forward unfiltered as written, but add a `--filter-list` flag** that strips `admin_*` for viewers, off by default. One extra function, strong interview talking point ("least privilege says yes; brief says no; I made it a switch").
3. **Where does the `id` in our intercepted error come from?** Must echo the request `id`. If the request `id` is unparseable → `null`. Rubric: *parsing wire format correctly*.
4. **Notifications** (no `id`): forward, never respond with a body. Make sure we don't fabricate an error response to a notification.
5. **Session header passthrough**: `Mcp-Session-Id` round-trips unchanged. Yes.

## Proposed module layout (names only)

```
task2-mcp-gateway/
├── pyproject.toml                 starlette · uvicorn · httpx · pydantic · pytest · pytest-asyncio
├── src/mcp_gateway/
│   ├── jsonrpc.py                 JsonRpcRequest / JsonRpcError / JsonRpcErrorResponse (Pydantic, extra=forbid where safe) · parse_request(bytes) -> Request | Error
│   ├── auth.py                    extract_bearer(headers) -> str | None · resolve_role(token) -> Role | None
│   ├── policy.py                  is_admin_tool(name) · authorize(role, method, params) -> Decision
│   ├── forward.py                 forward(req: Request, body: bytes) -> Response  (httpx, timeout, header filtering)
│   ├── app.py                     Starlette app: single POST /mcp route; the only place the pipeline is wired
│   ├── errors.py                  code constants + make_error(id, code, msg) -> JSONResponse
│   └── config.py                  DOWNSTREAM_URL, TOKEN_MAP_PATH, TIMEOUT_S
├── mock_downstream/
│   └── app.py                     tools/list → [get_customer, admin_reset_key]; tools/call → echoes
└── tests/
    ├── test_jsonrpc_parse.py      parse error, invalid request, missing params.name, batch rejection, notification
    ├── test_auth_policy.py        viewer+admin_ → -32001; admin+admin_ → forward; viewer+plain → forward; no header → 401
    ├── test_forward.py            downstream 500 → sanitized; timeout → sanitized; headers stripped; session id passes
    └── test_e2e.py                gateway ↔ mock downstream in-process via ASGITransport
```

## Pipeline order (this IS the middleware design — defend it)

```
POST /mcp
  1. extract_bearer → none/unknown → 401 + -32001          (fail fast, cheapest check first)
  2. parse_request  → -32700 / -32600 / -32602             (never forward garbage)
  3. authorize(role, method, params.name) → DENY → -32001  (before any network I/O)
  4. forward → sanitize upstream failures → -32000/-32603
  5. passthrough status, body, Content-Type, Mcp-Session-Id
```

Interview line: *"Auth before parse would leak parse errors to anonymous callers. Parse before authorize because policy needs `params.name`. Authorize before forward so a denied call never touches downstream."*

## Pressure Test Checklist

- [ ] `curl` with no header → 401, body `{"jsonrpc":"2.0","id":null,"error":{"code":-32001,...}}`
- [ ] viewer token + `tools/call admin_reset_key` → **downstream receives nothing** (assert mock call counter == 0)
- [ ] admin token + same call → forwarded, downstream response returned byte-identical
- [ ] viewer + `tools/call get_customer` → forwarded
- [ ] viewer + `tools/list` → forwarded, response unmodified (unless `--filter-list`)
- [ ] `params.name = "Admin_reset"` (capital A) → **grill**: case-sensitive match or not? Default: exact prefix, documented.
- [ ] `params.name = "admin_"` (just the prefix) → denied for viewer
- [ ] body `[{...},{...}]` (batch) → `-32600`
- [ ] body `{"jsonrpc":"1.0",...}` → `-32600`
- [ ] notification (no `id`) → forwarded, gateway returns 202/empty, no fabricated error
- [ ] downstream down → `-32000 Upstream unavailable`, no `httpx.ConnectError` text in body, no hostname
- [ ] downstream slow > timeout → same sanitized error, request actually cancelled
- [ ] `Connection`, `Transfer-Encoding`, `Host`, `Content-Length` are **not** forwarded verbatim
- [ ] `ruff check .` clean; `pytest -q` green; README has the error-code table

## Interview questions you will drill me on

- "Why `-32001`? Where does the server-defined range start and end?"
- "Why intercept at the gateway instead of letting downstream enforce auth?" (defense in depth; downstream may be third-party)
- "What stops a viewer from calling `tools/call` with `name: 'admin_reset_key'` but a different casing or unicode lookalike?" (normalize, exact match, deny-list is the weakness → allow-list is the upgrade)
- "How would you replace the static token map?" (JWT + JWKS, role claim, `aud` check)
- "What happens to streaming (SSE) responses from downstream?" (ponytail: buffered now; `StreamingResponse` passthrough is the upgrade — say so in README)
