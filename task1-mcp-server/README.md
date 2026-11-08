# Customer-ops MCP server

A stdio MCP server (official `mcp` SDK, `>=2,<3`) with two tools: `get_customer_record` and
`trigger_refund`. An LLM is an untrusted caller, and refunds move money, so every call passes two checks
before anything changes.

```bash
uv sync
uv run python mcp_server.py          # stdio server; silent until a client sends initialize
uv run pytest -q                     # unit, in-process client and real-subprocess stdout tests
uv run ruff check .
uv run mcp dev mcp_server.py         # MCP Inspector
```

| File | Holds |
|---|---|
| `models.py` | `CustomerId`, `Amount`, `Reason` (`Annotated` types) and the strict request models |
| `store.py` | in-memory customers and the refund rule; no I/O |
| `mcp_server.py` | the validation middleware, the `MCPServer` and the two tools |
| `tests/` | models, store, tools through a client, and stdio isolation over a subprocess |

### Stdio isolation

Stdout carries only JSON-RPC. The server prints nothing at import or at any later point, and the SDK logs
to stderr. `grep -rn "print(" --include='*.py' .` finds only a comment in the tests.
`tests/test_stdio_isolation.py` runs the real server over pipes and parses every stdout line as JSON-RPC
2.0. With empty stdin, the server exits cleanly and writes 0 bytes.

### Error strategy

| Failure | Example | Raised as | On the wire |
|---|---|---|---|
| Unknown tool | `delete_customer`, a missing or non-string `name` | `MCPError(INVALID_PARAMS)` | JSON-RPC `error`, code `-32602` |
| Format or schema | `cust-00001`, `amount: -5`, `amount: 0.005`, a 9-character reason, an extra field, `null` arguments | `MCPError(INVALID_PARAMS)` | JSON-RPC `error`, code `-32602` |
| Business rule | unknown customer, refund larger than the balance | `ToolError` | `result` with `isError: true` |

A format error means the request is broken, so the server rejects it at the protocol level and no tool
code runs. A business refusal is a valid request whose answer is "no": the model gets a readable message it
can explain or recover from. The error `data` lists field names and rule messages only. It never echoes the
caller's values back.

### Why middleware

The SDK validates tool arguments against the type hints before the tool runs, and turns a failure into an
`isError` result, not `-32602`. There are two ways to own the error code:

1. **Loose signatures** (`customer_id: str`) plus `model_validate` inside each tool. This works, but
   `tools/list` then advertises a plain `string`, and clients lose the constraints.
2. **Middleware** (chosen). `reject_malformed_arguments` runs before the SDK's check, validates the raw
   arguments against the same Pydantic models, and raises `-32602`. The tool signatures keep the strict
   types, so `inputSchema` still advertises `pattern`, `exclusiveMinimum`, `minLength` and
   `additionalProperties: false`.

One set of `Annotated` types in `models.py` drives the validator and the advertised schema, so the two
cannot drift apart. The cost: one middleware function, and a `REQUEST_MODELS` map that must list every tool.
That map is also the allowlist: a name not in it is rejected as an unknown tool, and a test checks that it
matches the registered tools.

### Validation rules

- `customer_id`: `^CUST-[0-9]{5}$`. `[0-9]` because `\d` also matches non-ASCII digits.
- `amount`: a strict float, greater than 0, at most 1,000,000, no `nan` or `inf`, and at most 2 decimal
  places. Strings and booleans are rejected. A sub-cent refund would make the receipt disagree with the
  balance, which is rounded to cents.
- `reason`: whitespace is stripped first, then 10–500 characters, of which at least 10 must be visible.
  Zero-width characters (Unicode category `Cf`) do not count.
- Extra fields are rejected (`extra="forbid"`).
- A refund larger than the refundable balance is refused, and the balance stays unchanged.

### Known limitation

A line that is not valid JSON, or a request whose `params` is not an object, gets **no reply**. JSON-RPC 2.0
expects `-32700` or `-32600`. The SDK's stdio transport drops these messages before any server code runs. The
server stays up and answers the next valid request, so the only cost is a client timeout on a request that
was already broken.
