# Task 1 — Customer-ops MCP Server (stdio, strict validation)

Guidance for working in this folder. The README is the reviewer-facing document; this file
records the task, the layout, the commands and the conventions the code follows.

## Task statement

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

A support assistant: an LLM handles a support conversation and reaches customer data only through
this server. The LLM is treated as an untrusted caller (it can hallucinate ids, flip signs or be
prompt-injected into sending extra fields), and refunds move money, so only well-formed requests
that follow the rules are accepted.

## Layout

| Path | Job |
|---|---|
| `models.py` | `CustomerId`, `Amount`, `Reason` (`Annotated` types), `GetCustomerRequest`, `RefundRequest` |
| `store.py` | In-memory `Customer` / `Refund` store and refund rules (pure, no I/O) |
| `mcp_server.py` | `reject_malformed_arguments` middleware, `MCPServer("customer-ops")`, the two tools |
| `tests/test_models.py` | Malformed ids, amounts, reasons; extra and missing fields |
| `tests/test_store.py` | Refund rules, rounding, zero balance |
| `tests/test_tools.py` | In-process `Client`: -32602 vs `isError`, no value echo, advertised schema |
| `tests/test_stdio_isolation.py` | Real subprocess over pipes: every stdout line is JSON-RPC |

## Commands

```bash
uv sync --locked
uv run pytest -q
uvx ruff@0.17.4 check .
uv run python mcp_server.py      # stdio server; silent until a client sends initialize
uv run mcp dev mcp_server.py     # MCP Inspector
```

## SDK

Official SDK, `mcp>=2,<3` (2.3.0 locked). In v2 the high-level class is `MCPServer`
(`from mcp.server import MCPServer`); `mcp.server.fastmcp` no longer exists. Result fields are
snake_case (`input_schema`, `is_error`, `structured_content`).

## Error strategy

| Failure | Example | Raised as | On the wire |
|---|---|---|---|
| Unknown tool | `delete_customer` | `MCPError(INVALID_PARAMS)` in middleware | JSON-RPC `error`, `-32602` |
| Format / schema | `cust-00001`, `amount: -5`, short reason, extra field, `null` arguments | `MCPError(INVALID_PARAMS)` in middleware | JSON-RPC `error`, `-32602` |
| Business rule | unknown customer, refund over balance | `ToolError` in the tool | `result` with `isError: true` |

The SDK validates arguments from the tool's type hints before the function runs and turns
failures into `isError`. `reject_malformed_arguments` (a `ServerMiddleware`) runs first, sees the
raw `ctx.method` / `ctx.params`, validates against the same Pydantic models and raises `-32602`.
The tool signatures keep their strict types, so `inputSchema` still advertises `pattern`,
`exclusiveMinimum`, `minLength` and `additionalProperties: false`. `REQUEST_MODELS` is the
allowlist of tool names; a test keeps it equal to the registered tools.

## Validation rules

| Field | Rule |
|---|---|
| `customer_id` | `^CUST-[0-9]{5}$` (`[0-9]`, not `\d`, which matches non-ASCII digits) |
| `amount` | strict float, `> 0`, `<= 1_000_000`, no inf/nan, at most 2 decimals |
| `reason` | stripped, 10–500 characters, at least 10 that are not invisible (category Cf) |
| Extra fields | rejected (`extra="forbid"`) |
| Over-balance refund | rejected, balance unchanged; check and write with no `await` between |

## Conventions

- **stdout carries only JSON-RPC.** Logging goes to stderr; no `print` in server code, and
  importing a module writes nothing. `test_stdio_isolation.py` fails on any stray output.
- **Error `data` holds field names and rule messages only,** never the caller's values.
- **One set of types** in `models.py` drives both the validator and the advertised schema.
- **Tests** use the anyio pytest plugin; `conftest.py` resets the store for every test.
- **Known limitation:** a line that is not valid JSON, or `params` that is not an object, is
  dropped by the SDK's stdio transport before server code runs (documented in the README).
