# Quilr Solution Architect take-home

Four small, independent Python programs. Each one answers one task of the assessment. They share a toolchain but no
code: each folder has its own `pyproject.toml`, lock file, tests and README.

| Folder | Task | What it does |
|---|---|---|
| [`task1-mcp-server/`](task1-mcp-server/) | Custom MCP server | A stdio MCP server with `get_customer_record` and `trigger_refund`. Malformed input returns JSON-RPC `-32602`; a refused refund returns an `isError` result. Stdout carries only JSON-RPC. |
| [`task2-mcp-gateway/`](task2-mcp-gateway/) | MCP security gateway | An HTTP reverse proxy in front of an MCP server. It reads the role from a `Bearer` token and blocks `admin_*` tool calls for non-admins with `-32001`, without calling the downstream server. |
| [`task3-llm-stream-guardrail/`](task3-llm-stream-guardrail/) | Streaming PII guardrail | An LLM gateway endpoint that streams the answer back and replaces emails, SSNs and card numbers with `[REDACTED]`, including values split across chunks, while holding back only a small bounded tail. |
| [`task4-rate-limit-fallback-router/`](task4-rate-limit-fallback-router/) | Rate limiter and fallback router | A token-aware sliding-window limit per API key (50,000 tokens per minute) in on-disk SQLite, failover to a second provider on `429` or a 3000 ms timeout, and one sanitized error payload. |

Each task's README explains its design, its error table and its known limits.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)

No API keys are needed. The tests and demos use built-in mock servers and fake providers.

## Run a task

Every task follows the same steps from inside its folder:

```bash
cd task2-mcp-gateway        # or any other task folder
uv sync                     # install the locked dependencies
uv run pytest -q            # run the tests
uvx ruff@0.17.4 check .     # lint
```

Tasks 2, 3 and 4 also have a probe script. It starts the program and sends hostile input to it, for example
malformed JSON, split personal data, races for the last token, or timeouts:

```bash
uv run python probe.py
```

The task READMEs give the command that starts each server for a manual demo.

## Shared design rules

- **Validate at the boundary.** Input is checked once, where it enters, with strict Pydantic models
  (`strict=True`, `extra="forbid"`). Bad input never reaches business logic.
- **Errors leak nothing.** Error responses use fixed messages. They never echo caller values or carry upstream
  hostnames, stack traces or provider text.
- **Tests prove the guards.** Each guard has a test that fails when the guard is removed. Each probe script
  covers input that the unit tests did not plan for.

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs `uv sync --locked`, `pytest` and `ruff` for every
task folder on each push to `main` and on each pull request.
