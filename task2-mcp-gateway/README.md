# Task 2 — MCP Security Gateway

An HTTP reverse proxy that sits between an AI agent and a downstream MCP server.
It reads the caller's role from a Bearer token and blocks `admin_*` tool calls for non-admins
**before** anything reaches the downstream server. Everything else is forwarded unchanged.

## Run it

```bash
uv sync
uv run pytest -q                 # 100 tests, all in-process
uvx ruff@0.17.4 check .
uv run python probe.py           # 41 hostile inputs against the gateway under real uvicorn
```

Start the mock downstream and the gateway in two terminals:

```bash
uv run uvicorn --factory mock_downstream.app:create_mock --port 9000
```

```bash
GATEWAY_DOWNSTREAM_URL=http://127.0.0.1:9000/mcp \
GATEWAY_TOKENS='{"tok_admin_demo":"admin","tok_viewer_demo":"viewer"}' \
uv run uvicorn --factory mcp_gateway.app:app_from_env --port 8000
```

A viewer calling an admin tool is stopped at the gateway:

```bash
curl -s localhost:8000/mcp -H 'Authorization: Bearer tok_viewer_demo' \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key"}}'
# {"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Unauthorized Tool Call"}}
```

`GATEWAY_TIMEOUT_S` (default 10) sets the downstream deadline. The demo tokens are test values.

## Request flow

```
client ──POST /mcp──▶ gateway ──▶ downstream MCP server
                        │
   1. Bearer token → role         (none/unknown → 401)
   2. Parse JSON-RPC envelope     (-32700 / -32600)
   3. Authorize tools/call        (viewer + admin_* → -32001, downstream untouched)
   4. Forward, sanitize failures, pass the response through
```

The order is the design. Authentication runs before parsing, so an anonymous caller learns
nothing about the parser. Parsing runs before authorization, because the policy needs
`params.name`. Authorization runs before forwarding, so a denied call never touches the downstream.

| File | Job |
|---|---|
| `mcp_gateway/auth.py` | `extract_bearer` (RFC 6750 grammar) and `resolve_role` |
| `mcp_gateway/jsonrpc.py` | `parse_request`: bytes → strict `JsonRpcRequest`, or a JSON-RPC error |
| `mcp_gateway/policy.py` | `authorize(role, request)` → allow / deny / invalid_params |
| `mcp_gateway/forward.py` | `forward`: header allowlist, deadline, pass-through or sanitized error |
| `mcp_gateway/errors.py` | Error codes and the one error builder |
| `mcp_gateway/app.py` | The Starlette route that wires the four steps |
| `mock_downstream/app.py` | Mock MCP server with call counters (tests and probe only) |

## Error codes

| Situation | HTTP | Code | Message |
|---|---|---|---|
| Missing/invalid Bearer token | 401 + `WWW-Authenticate: Bearer` | `-32001` (`id: null`) | Unauthorized |
| Body not JSON (also `NaN`, invalid UTF-8, nesting too deep) | 200 | `-32700` | Parse error |
| Not one JSON-RPC 2.0 request: batch, scalar, repeated key, bad `jsonrpc`/`id`/`params` | 200 | `-32600` | Invalid Request |
| `tools/call` with `params.name` missing, not a string, or containing invisible (Cf) characters | 200 | `-32602` | Invalid params |
| Non-admin calls an `admin_*` tool | 200 | `-32001` | Unauthorized Tool Call |
| The same, sent as a notification (no `id`) | 403 | — | no body (400 for invalid params) |
| Downstream unreachable | 502 | `-32603` | Upstream unavailable (`data.reason: unreachable`) |
| Downstream timeout | 504 | `-32603` | Upstream unavailable (`data.reason: timeout`) |
| Downstream answers with something that is not JSON-RPC (e.g. an HTML 500) | 502 | `-32603` | Upstream unavailable (`data.reason: bad_response`) |
| Downstream JSON-RPC answer, including its own errors | passed through | theirs | byte-identical |

Errors echo the request `id` when it can be read as a string or integer, and use `null` otherwise
(JSON-RPC 2.0 §5).

## Design decisions

- **Spec target: MCP 2026-07-28** (Streamable HTTP). `Mcp-Session-Id` from older clients passes
  through both ways.
- **`-32001` is in a legacy range.** MCP 2026-07-28 says new implementations SHOULD NOT use
  `-32000..-32019`. The brief requires `-32001`, so it stays, for the two auth cases only.
  Upstream failures use the standard `-32603` instead of `-32000`.
- **401 plus a JSON-RPC body.** The status and `WWW-Authenticate` serve HTTP clients (RFC 6750 §3),
  and the body gives MCP clients something to parse. "Unauthorized" (who are you) is kept apart
  from "Unauthorized Tool Call" (you may not do that).
- **`admin_` matching fails closed.** The name is compared after NFKC normalization and casefold,
  so `Admin_x`, `ADMIN_X` and fullwidth `ａｄｍｉｎ_x` are all admin tools. The method gets the same
  treatment (`TOOLS/CALL`). NFKC keeps invisible format characters, so a name holding one is
  rejected outright. The original name is forwarded unchanged.
- **Duplicate JSON keys are rejected.** Python keeps the last copy of a repeated key, while other
  parsers keep the first. Without this, `{"name":"get_customer","name":"admin_reset_key"}` could
  be allowed here and run as admin downstream. With duplicates gone, forwarding the original bytes
  is safe.
- **Strict envelope.** `strict=True` stops `"id": true` from becoming `1`. An absent `id` marks a
  notification, while `"id": null` is invalid, because MCP forbids null ids.
- **Notifications go through the same policy.** Skipping id-less messages would let a viewer drop
  the `id` and run an admin tool. A denied notification gets a status code with no body, as the
  Streamable HTTP spec requires.
- **Header allowlist and no token passthrough.** Only `Content-Type`, `Accept` and `Mcp-*`
  headers go downstream. Hop-by-hop headers (`Connection`, `TE`, `Transfer-Encoding`, `Host`,
  `Content-Length`) are rebuilt, and `Authorization` is never forwarded: the gateway's token is not
  the downstream's credential.
- **Sanitized upstream failures.** `data.reason` is one fixed word chosen by the kind of
  failure, never exception text, which can name hosts and ports.
- **The deadline uses `anyio.fail_after`.** httpx timeouts are not enforced on the in-process
  `ASGITransport` (measured: a 0.1 s timeout returned a 200 after 1 s). `fail_after` cancels the
  call on any transport, and a test asserts that the slow downstream call never completed.
- **`tools/list` is not filtered.** The brief asks for transparent forwarding, and the
  `tools/call` deny is the real control.

Sources: [JSON-RPC 2.0](https://www.jsonrpc.org/specification);
[MCP 2026-07-28 basic](https://modelcontextprotocol.io/specification/2026-07-28/basic/index)
(ids, error ranges); [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
(202 for notifications); [schema `CallToolResult`](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2026-07-28/schema.ts)
(unknown tool is a protocol error).

## Known limits

- **Static token map.** Swap for JWT verification (JWKS, `aud`, a role claim) in production.
- **No request body size cap.** Starlette reads the whole body; add a limit at the edge.
- **SSE is buffered, not streamed.** A `text/event-stream` answer passes through only once it
  is complete, so progress events arrive late. The upgrade is a `StreamingResponse` pass-through.
- **The deadline covers the whole buffered answer.** A tool call (including a long SSE stream)
  that runs longer than `GATEWAY_TIMEOUT_S` gets a 504, even if the downstream is still working.
- **Client JSON-RPC responses are rejected with `-32600`.** A body such as
  `{"jsonrpc":"2.0","id":1,"result":{}}` has no `method`. MCP 2026-07-28 forbids clients from
  POSTing responses, but a 2025-11-25 client answering a server request (sampling, elicitation)
  would be refused. Forwarding responses unchanged is the upgrade.
- **POST only.** `GET /mcp` (a server-to-client stream) returns 405.
- **An invisible character inside the method** (`tools/​call`) skips the policy. The
  downstream then sees an unknown method; rejecting it would need a method-level rule.
- **No rate limiting or audit log.**

## Upgrades (deliberately not built)

- **Hide `admin_*` from viewers in `tools/list`.** Least privilege says yes. Not built because the
  brief requires transparent forwarding. The `tools/call` deny is the real control: a viewer who
  knows the name still gets `-32001`.
- **Allow-list per role instead of an `admin_` deny-prefix.** The prefix check normalizes (NFKC +
  casefold) and rejects invisible characters, but it can't catch confusables such as Cyrillic `а`.
  A per-role allow-list closes that class of bypass entirely.
  The invisible-character check also covers only category Cf: a combining mark such as U+034F
  (category Mn) inside `admin_` passes as a non-admin name. That is a bypass only if the
  downstream strips default-ignorable characters before looking up the tool; the allow-list
  closes it too.
- **Check `Mcp-Method` / `Mcp-Name` headers against the body.** MCP 2026-07-28 requires a server to
  reject a mismatch with HTTP 400 + `-32020`, so a component routing on the header and one
  executing the body cannot disagree. The gateway authorizes on the body and forwards the headers
  unchanged; a 2026-07-28 downstream still enforces the check itself.
