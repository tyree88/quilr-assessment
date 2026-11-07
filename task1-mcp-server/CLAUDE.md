# Task 1 — Custom MCP Server (stdio, strict validation)

Root `CLAUDE.md` rules apply. This file adds task context.
**Format: take-home AND 60-min live coding.** Every line must be reproducible from memory under a clock.

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

## Library decision (settled — do not reopen without a rubric reason)

**Use the official SDK: `mcp>=2,<3`.** Not standalone `fastmcp` (Prefect).

Why:
- Rubric says *"official SDK (`mcp`)"*. Standalone `fastmcp` 3.x/4.x is a separate Prefect project. Using it risks a "did not follow the brief" mark.
- In `mcp` v2 the high-level class is **`MCPServer`** (`from mcp.server import MCPServer`). `FastMCP` was renamed. The old `mcp.server.fastmcp` import path is **removed**, not deprecated → `ModuleNotFoundError`.
- Pin `mcp>=2,<3`. Unpinned `mcp` resolves to 2.x today; `mcp<2` docs live under `/v1/`. Mixing them is the #1 way to lose an hour live.

**Verify on entry:** `uv run python -c "from mcp.server import MCPServer; import mcp; print(mcp.__version__)"`.

## Docs to pull on entry (Context7: `/modelcontextprotocol/python-sdk/v2.3.0` or current)

1. `docs/servers/tools.md` — `@mcp.tool()`, `Annotated[..., Field(...)]`, Pydantic model params
2. `docs/servers/handling-errors.md` — `ToolError` vs `MCPError`, `mcp.types.INVALID_PARAMS`
3. `docs/run/index.md` — stdio: *"stdout is the wire"*, what still leaks pre-serve
4. `docs/handlers/logging.md` — stderr logging handler
5. MCP spec, tools section — `isError` result vs JSON-RPC `error` object

## The one design tension you must grill me on first

**The rubric says "reject invalid formats with standard MCP JSON-RPC error codes."**
**The SDK default does the opposite.**

- Default behavior: when Pydantic rejects tool arguments, the SDK returns a *result* with `is_error=True` and a readable message. That is a JSON-RPC **success** envelope. No `-32602`.
- SDK guidance: `ToolError` for *execution* failures the model can fix; `MCPError` for requests that should be *rejected* at protocol level. `MCPError(code=INVALID_PARAMS, ...)` propagates verbatim as a JSON-RPC `error` object with `-32602`.

Options on the table:
1. **Trust SDK default** (`is_error` result). Simplest. Loses the "JSON-RPC error codes" rubric line.
2. **Raise `MCPError(INVALID_PARAMS)` from inside each tool** after our own Pydantic check. Hits rubric literally. Slightly fights the SDK's model-recoverability philosophy.
3. **Hybrid**: format violations (`CUST-XXXXX`, negative amount, short reason) → `MCPError -32602`; *business* failures (customer not found, refund exceeds balance) → `ToolError` / `is_error`. Two layers, each with a one-sentence justification.

**Implementation trap for options 2/3:** the SDK validates arguments against the type hints *before* your function runs. If `customer_id: CustomerId` carries the regex, a bad id never reaches your code → SDK emits `is_error`, not `-32602`. To own the error code, the tool signature takes the loose type (`customer_id: str`, `amount: float`, `reason: str`) and the **first line** of the tool body runs `RefundRequest.model_validate(...)` inside a `try` → `MCPError(INVALID_PARAMS, ...)`. The strict Pydantic models still exist and still drive tests; they just run one layer in. Mention this trade-off in the README — it is a strong interview answer.

**Recommended: option 3.** It scores both "Protocol Compliance" (real `-32602` on malformed input) and "Validation" (edge cases handled, not just rejected), and gives a crisp interview answer: *"Schema violations are protocol errors; domain failures are tool errors the model can recover from."*
Confirm or override before any code.

## Proposed module layout (names only — bodies get pair-built)

```
task1-mcp-server/
├── pyproject.toml                 mcp>=2,<3 · pydantic>=2 · pytest · pytest-asyncio
├── src/customer_mcp/
│   ├── __init__.py
│   ├── models.py                  CustomerId (Annotated str, regex ^CUST-\d{5}$) · RefundRequest
│   ├── store.py                   in-memory dict of 3 fake customers; no I/O, no logging to stdout
│   ├── errors.py                  invalid_params(msg) -> MCPError(INVALID_PARAMS, ...)
│   ├── server.py                  mcp = MCPServer("customer-ops") · two @mcp.tool() · __main__ guard
│   └── logging_setup.py           logging.basicConfig(stream=sys.stderr, ...) · called before anything else
└── tests/
    ├── test_models.py             param cases: valid, lowercase, 4 digits, 6 digits, whitespace, None, int
    ├── test_tools.py              in-process client via mcp client API; asserts -32602 vs is_error paths
    └── test_stdio_isolation.py    subprocess: send initialize + tools/list; every stdout line parses as JSON-RPC
```

## Grill checklist (ask me these, one at a time, with your pick)

1. `CUST-XXXXX`: X = digit only, or alphanumeric? → **Digits** (`\d{5}`); state assumption in tool description.
2. `amount` positive float: reject `0`? reject `inf`/`nan`? max? → `gt=0`, `allow_inf_nan=False`, `le=1_000_000` ponytail cap.
3. `reason` min length 10: before or after `strip()`? → `strip` first, then `min_length=10`. Document it.
4. Unknown customer on `trigger_refund`: `MCPError` or `ToolError`? → `ToolError` (model can retry with correct id).
5. Return shape: plain `dict` → structured content, or explicit `CallToolResult`? → Pydantic return model → structured output for free.
6. Extra fields in input: ignore or reject? → `extra="forbid"` on models. Rubric says *strict*.

## Pressure Test Checklist (prove each with a command or test)

- [ ] `uv run python -m customer_mcp.server < /dev/null` exits clean, **zero bytes on stdout** before the host speaks
- [ ] `test_stdio_isolation.py`: spawn server, send `initialize`, `notifications/initialized`, `tools/list`; assert every stdout line is valid JSON with `"jsonrpc":"2.0"`
- [ ] `grep -rn "print(" src/` returns nothing
- [ ] Import-time side effects: no logging/print at module import (SDK only diverts stdout *after* serving starts)
- [ ] `tools/call get_customer_record {"customer_id":"cust-00001"}` → JSON-RPC `error.code == -32602`
- [ ] `tools/call trigger_refund {"amount": -5, ...}` → `-32602`
- [ ] `tools/call trigger_refund {"reason": "too short", ...}` → `-32602`
- [ ] `tools/call trigger_refund` for unknown-but-well-formed id → result with `is_error: true`
- [ ] `tools/call` with `arguments: null` does not crash the server
- [ ] `tools/list` output: each tool's `inputSchema` still **advertises** the constraints — use `Annotated[str, Field(pattern=..., description=...)]` with `json_schema_extra` or `WithJsonSchema` so the schema shows `pattern` / `exclusiveMinimum` / `minLength` while our in-body validation owns the `-32602`. **Grill**: if the SDK then pre-validates and short-circuits, fall back to description-only and document why.
- [ ] Works with `uv run mcp dev src/customer_mcp/server.py` (Inspector) — screenshot-able proof
- [ ] `ruff check .` clean; `pytest -q` green

## Interview questions you will drill me on

- "Why `MCPError` for format errors and `ToolError` for not-found?"
- "What exactly does the SDK do to stdout during serving, and what still leaks?"
- "Why pin `mcp>=2,<3`? What breaks on v1?"
- "Where would auth go if this were HTTP instead of stdio?"
- "How does the client learn the `CUST-XXXXX` constraint before calling?" (→ `inputSchema.pattern`)
- "Show me the regex. What does it reject that a naive `startswith('CUST-')` would accept?"

## Live-coding (60 min) time budget

| Minute | Deliverable |
|---|---|
| 0–5 | `uv init`, pin deps, `logging_setup.py`, empty `MCPServer` runs |
| 5–20 | `models.py` + `test_models.py` (param cases) |
| 20–35 | two tools + `errors.py` + `store.py` |
| 35–45 | `test_tools.py` (−32602 path + is_error path) |
| 45–55 | stdio isolation test |
| 55–60 | Inspector demo, `ruff`, commit |

If behind at minute 35: cut `test_stdio_isolation.py` to a manual `grep` + one `echo | python` demo. Never cut the models or the `-32602` path.
