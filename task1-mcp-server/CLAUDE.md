# Task 1 — Custom MCP Server (stdio, strict validation)

Root `CLAUDE.md` rules apply. This file adds task context.
**Format: take-home AND 60-min live coding.** Every line must be reproducible from memory under a clock.

**Status:** Phases 0–2 done, Phase 3 pressure test done except the Inspector UI check. Phase 4 (`INTERVIEW_NOTES.md`) and the README error-strategy section are next.

## Task statement (verbatim from assessment)

> Write a runnable MCP server (in TypeScript or Python) using the official SDK (`@modelcontextprotocol/sdk` or `mcp`) that exposes two tools:
> - `get_customer_record` (Input: `customer_id` string formatted as `CUST-XXXXX`).
> - `trigger_refund` (Inputs: `customer_id`, `amount` positive float, `reason` string with minimum length of 10).
>
> Requirements:
> - Enforce strict input schema validation (using Zod or Pydantic). Reject invalid formats with standard MCP JSON-RPC error codes.
> - Connect via stdio transport. Ensure stdout is strictly reserved for JSON-RPC messages and system/debug logs write exclusively to stderr.
>
> Evaluation criteria:
> - **STDIO Isolation**: stdout is pure JSON-RPC, no accidental `console.log` / `print`.
> - **Protocol Compliance**: standard JSON-RPC error mapping and execution flow.
> - **Validation**: robust schema definitions and edge-case handling for malformed input fields.

## Use case

A support assistant: an LLM handles a support conversation and reaches customer data only through this server. **The LLM is an untrusted caller.** It can hallucinate ids, flip signs, or be prompt-injected into sending extra fields. Refunds move money, so the server accepts only well-formed requests that follow the rules.

## Library decision (settled — do not reopen without a rubric reason)

**Official SDK, `mcp>=2,<3` (2.3.0 locked).** Not standalone `fastmcp` (Prefect).

- The rubric says *"official SDK (`mcp`)"*. Standalone `fastmcp` is a separate project and risks a "did not follow the brief" mark.
- In v2 the high-level class is **`MCPServer`** (`from mcp.server import MCPServer`). `mcp.server.fastmcp` is **removed** → `ModuleNotFoundError`.
- **v1 cannot produce -32602 from a tool**: every tool exception becomes an `isError` result. That alone justifies v2.

**Verify on entry:** `uv run python -c "from mcp.server import MCPServer; import mcp; print(mcp.__version__)"`.

## Design (built)

### Error strategy: hybrid, enforced by middleware

| Failure | Example | Raised as | Wire shape |
|---|---|---|---|
| Format / schema | `cust-00001`, `amount: -5`, short reason, extra field, `null` args | `MCPError(code=INVALID_PARAMS)` in middleware | JSON-RPC `error`, code **-32602** |
| Domain | unknown customer, refund over balance | `ToolError` in the tool | `result` with `isError: true` |

**Interview line:** *"Schema violations are protocol errors; domain failures are tool errors the model can recover from."*

### Why middleware, not in-body validation (the trap we hit)

The SDK validates arguments against the tool's type hints **before** the function runs and turns failures into `isError` (`mcp/server/mcpserver/server.py` ~440–455). The original plan was loose signatures (`customer_id: str`) plus `model_validate` in the tool body. That works, but the advertised `inputSchema` then loses its constraints.

Chosen fix: **`ServerMiddleware`** (`MCPServer(..., middleware=[fn])`, see `mcp/server/context.py` ~146–162). It runs before the SDK's check and sees the raw `ctx.method` / `ctx.params`. It validates against the same Pydantic request model and raises `MCPError(-32602)`. Result: **strict signatures** (the schema advertises `pattern` / `exclusiveMinimum` / `minLength`) **and** a real -32602, from one set of types.

### Layout (flat, inside the course starter)

```
task1-mcp-server/
├── pyproject.toml        mcp[cli]>=2,<3 · pytest (dev) · [tool.ruff] excludes starter core/ and main.py
├── models.py             CustomerId · Amount · Reason (Annotated) · GetCustomerRequest · RefundRequest
├── store.py              Customer · Refund · InsufficientBalance · get_customer · record_refund (pure, no I/O)
├── mcp_server.py         reject_malformed_arguments middleware · MCPServer("customer-ops", version="0.1.0") · 2 tools
├── mcp_client.py         starter client; list_tools/call_tool finished (prompts/resources still TODO)
├── core/, main.py        starter chat app (Anthropic course) — not graded, lint-excluded
└── tests/
    ├── conftest.py               anyio_backend fixture · autouse store reset
    ├── test_store.py             refund rules, rounding, zero balance
    ├── test_models.py            parametrized malformed ids / amounts / reasons, extra + missing fields
    ├── test_tools.py             in-process Client(mcp): -32602 vs isError, no value echo, schema advertises constraints
    └── test_stdio_isolation.py   real subprocess over pipes; stdin kept open until each reply arrives
```

### Decisions log

| Decision | Choice | Why |
|---|---|---|
| `CUST-XXXXX` | `^CUST-[0-9]{5}$` | `[0-9]` not `\d` (`\d` matches Arabic-Indic digits); anchored both ends |
| `amount` | `gt=0`, `le=1_000_000`, `allow_inf_nan=False`, strict float | 0, nan, inf, `"10"` and `True` rejected; cap marked as a shortcut |
| `reason` | strip, then `min_length=10`, `max_length=500` | `"   short    "` must fail |
| Extra fields | `extra="forbid"`, advertised as `additionalProperties: false` | rubric says strict; injected `approved_by` rejected |
| Unknown customer | `ToolError` | well-formed request, the model can retry |
| Over-balance refund | reject (option A), balance unchanged | no partial refunds; check + write with no `await` between |
| Error `data` | field names + rule messages only | never echo caller values back |
| Return shape | Pydantic models → `structuredContent` + `outputSchema` | structured output for free |
| Tests | anyio pytest plugin, no pytest-asyncio | anyio ships with `mcp` |
| `additionalProperties` | set on the registered tool after decoration | no public SDK hook; private `_tool_manager`, marked `shortcut:` |

## Pressure Test Checklist (status from the 2026-11 run)

- [x] `python mcp_server.py </dev/null` → exit 0, **0 bytes** on stdout and stderr
- [x] `test_every_stdout_line_is_jsonrpc`: handshake, tools/list, -32602 ×2, isError, valid refund — every line JSON-RPC 2.0
- [x] `grep -n "print(" mcp_server.py store.py models.py` → empty (the starter's `core/` prints run in the client process; scope documented in the README)
- [x] `python -c "import mcp_server"` → 0 bytes out (no import-time side effects)
- [x] `cust-00001` / `amount: -5` / `reason: "too short"` → `-32602` (tests + manual stdio run)
- [x] well-formed unknown id → `isError: true`
- [x] `arguments: null` → `-32602 "Field required"`, server keeps running
- [x] `inputSchema` advertises `pattern` / `exclusiveMinimum` / `minLength` / `additionalProperties: false`
- [x] Mutation check: a stray `print` at import or inside a tool makes the stdio test fail
- [x] `mcp run mcp_server.py </dev/null` → exit 0, 0 bytes (the Inspector's launch path imports `store`/`models`)
- [ ] Inspector UI: `uv run mcp dev mcp_server.py` → Connect → run the 4 cases, screenshot
- [x] `ruff check .` clean; `pytest -q` → 51 passed

## Interview questions you will drill me on

- "Why `MCPError` for format errors and `ToolError` for not-found?"
- "The SDK already validates arguments. Why the middleware?" (→ it would emit `isError`, not -32602)
- "What exactly does the SDK do to stdout during serving, and what still leaks?"
- "`grep` finds `print()` in `core/`. You said stdout is pure JSON-RPC." (→ client process, not the server pipe)
- "Why pin `mcp>=2,<3`? What breaks on v1?"
- "Where would auth go if this were HTTP instead of stdio?"
- "How does the client learn the `CUST-XXXXX` constraint before calling?" (→ `inputSchema.pattern`)
- "Show me the regex. What does it reject that a naive `startswith('CUST-')` or `\d` would accept?"

## Live-coding (60 min) time budget

| Minute | Deliverable |
|---|---|
| 0–5 | `uv init`, pin `mcp[cli]>=2,<3`, empty `MCPServer` runs silent on `</dev/null` |
| 5–20 | `models.py` + `test_models.py` (param cases) |
| 20–30 | `store.py` + the two tools with `ToolError` |
| 30–40 | middleware → -32602; `test_tools.py` (-32602 path + isError path) |
| 40–50 | stdio isolation test (keep stdin open) |
| 50–60 | Inspector demo, `ruff`, commit |

If behind at minute 40: cut `test_stdio_isolation.py` to a manual `printf … | python mcp_server.py` demo. Never cut the models or the -32602 path.
