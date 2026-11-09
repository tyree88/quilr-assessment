"""Hostile-input probe: run the gateway and the mock under real uvicorn and attack it over sockets.

Usage: uv run python probe.py    (exit code 0 = every case behaved as expected)
"""

import json
import os
import socket
import subprocess
import sys
import time

import httpx

TOKENS = {"tok_admin_demo": "admin", "tok_viewer_demo": "viewer"}
ADMIN = {"Authorization": "Bearer tok_admin_demo"}
VIEWER = {"Authorization": "Bearer tok_viewer_demo"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rpc(method="tools/call", params=None, id=1, **extra) -> bytes:
    message = {"jsonrpc": "2.0", "method": method, **extra}
    if id is not None:
        message["id"] = id
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode()


def call(name, **kw) -> bytes:
    return rpc(params={"name": name, "arguments": {"customer_id": "c1"}}, **kw)


# (label, headers, body, expected HTTP status, expected JSON-RPC error code or None, reaches downstream)
CASES = [
    ("no Authorization header", {}, call("get_customer"), 401, -32001, False),
    ("Basic scheme", {"Authorization": "Basic dXNlcjpwdw=="}, call("get_customer"), 401, -32001, False),
    ("token with a second word", {"Authorization": "Bearer tok_admin_demo x"}, call("admin_reset_key"), 401, -32001, False),
    ("unknown token + garbage body", {"Authorization": "Bearer guess"}, b"{", 401, -32001, False),
    ("lowercase bearer scheme", {"Authorization": "bearer tok_viewer_demo"}, rpc("tools/list"), 200, None, True),
    ("viewer tools/list", VIEWER, rpc("tools/list"), 200, None, True),
    ("viewer get_customer", VIEWER, call("get_customer"), 200, None, True),
    ("viewer admin_reset_key", VIEWER, call("admin_reset_key"), 200, -32001, False),
    ("admin admin_reset_key", ADMIN, call("admin_reset_key"), 200, None, True),
    ("viewer Admin_reset_key", VIEWER, call("Admin_reset_key"), 200, -32001, False),
    ("viewer just the prefix admin_", VIEWER, call("admin_"), 200, -32001, False),
    ("viewer fullwidth admin_ (JSON escape)", VIEWER, b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"\\uff41\\uff44\\uff4d\\uff49\\uff4e_reset_key"}}', 200, -32001, False),
    ("viewer zero-width space in name", VIEWER, b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"\\u200badmin_reset_key"}}', 200, -32602, False),
    ("viewer method TOOLS/CALL", VIEWER, rpc("TOOLS/CALL", {"name": "admin_reset_key"}), 200, -32001, False),
    ("viewer 100k-char admin_ name", VIEWER, call("admin_" + "x" * 100_000), 200, -32001, False),
    ("lying Mcp-Name header", {**VIEWER, "Mcp-Name": "get_customer"}, call("admin_reset_key"), 200, -32001, False),
    ("duplicate name key", VIEWER, b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","name":"get_customer"}}', 200, -32600, False),
    ("params is a list", VIEWER, b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":["admin_reset_key"]}', 200, -32600, False),
    ("params is a string", VIEWER, b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":"admin_reset_key"}', 200, -32600, False),
    ("name is a list", VIEWER, call(["admin_reset_key"]), 200, -32602, False),
    ("name is an int", VIEWER, call(42), 200, -32602, False),
    ("tools/call without params", VIEWER, rpc(), 200, -32602, False),
    ("id is true", VIEWER, rpc("tools/list", id=True), 200, -32600, False),
    ("id is a float", VIEWER, rpc("tools/list", id=1.5), 200, -32600, False),
    ("id is null", VIEWER, b'{"jsonrpc":"2.0","id":null,"method":"tools/list"}', 200, -32600, False),
    ("huge integer id", VIEWER, rpc("tools/list", id=10**30), 200, None, True),
    ("non-ASCII digit string id", VIEWER, rpc("tools/list", id="\u0661\u0662\u0663"), 200, None, True),
    ("unknown top-level field", VIEWER, rpc("tools/list", extra=1), 200, -32600, False),
    ("nested extra fields in arguments", VIEWER, rpc(params={"name": "get_customer", "arguments": {"a": {"b": [1, {"c": None}]}}}), 200, None, True),
    ("jsonrpc 1.0", VIEWER, b'{"jsonrpc":"1.0","id":1,"method":"tools/list"}', 200, -32600, False),
    ("batch array", VIEWER, b'[{"jsonrpc":"2.0","id":1,"method":"tools/list"}]', 200, -32600, False),
    ("malformed JSON", VIEWER, b'{"jsonrpc":', 200, -32700, False),
    ("empty body", VIEWER, b"", 200, -32700, False),
    ("NaN body", VIEWER, b"NaN", 200, -32700, False),
    ("deeply nested body", VIEWER, b"[" * 100_000, 200, -32700, False),
    ("invalid UTF-8", VIEWER, b"\xff\xfe{", 200, -32700, False),
    ("unknown method (downstream answers)", VIEWER, rpc("resources/list"), 200, -32601, True),
    ("unknown tool (downstream answers)", VIEWER, call("nope"), 200, -32602, True),
    ("viewer admin_ notification", VIEWER, call("admin_reset_key", id=None), 403, None, False),
    ("viewer allowed notification", VIEWER, rpc("notifications/initialized", id=None), 202, None, True),
]


def start(args: list[str], env: dict[str, str], port: int) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "uvicorn", *args, "--port", str(port), "--log-level", "warning"]
    return subprocess.Popen(cmd, env={**os.environ, **env})


def wait_up(url: str) -> None:
    for _ in range(100):
        try:
            httpx.get(url, timeout=0.2)
            return
        except httpx.TransportError:
            time.sleep(0.1)
    raise SystemExit(f"server at {url} did not start")


def judge(resp: httpx.Response, status: int, code: int | None, forwarded: bool, moved: bool) -> str:
    problems = []
    if resp.status_code != status:
        problems.append(f"status {resp.status_code}")
    got = None
    if resp.content:
        try:
            got = (resp.json().get("error") or {}).get("code")
        except ValueError:
            problems.append("non-JSON body")
    if got != code:
        problems.append(f"code {got}")
    if moved != forwarded:
        problems.append("reached downstream" if moved else "not forwarded")
    if b"Traceback" in resp.content or b"httpx" in resp.content:
        problems.append("leaks internals")
    return "PASS" if not problems else "FAIL " + ", ".join(problems)


def main() -> int:
    mock_port, gw_port, dead_port, dead_gw_port = (free_port() for _ in range(4))
    gw_env = {"GATEWAY_TOKENS": json.dumps(TOKENS), "GATEWAY_TIMEOUT_S": "2"}
    procs = [
        start(["--factory", "mock_downstream.app:create_mock"], {}, mock_port),
        start(
            ["--factory", "mcp_gateway.app:app_from_env"],
            {**gw_env, "GATEWAY_DOWNSTREAM_URL": f"http://127.0.0.1:{mock_port}/mcp"},
            gw_port,
        ),
        start(
            ["--factory", "mcp_gateway.app:app_from_env"],
            {**gw_env, "GATEWAY_DOWNSTREAM_URL": f"http://127.0.0.1:{dead_port}/mcp"},
            dead_gw_port,
        ),
    ]
    try:
        mock_calls = f"http://127.0.0.1:{mock_port}/_calls"
        for url in (mock_calls, f"http://127.0.0.1:{gw_port}/mcp", f"http://127.0.0.1:{dead_gw_port}/mcp"):
            wait_up(url)
        failures = 0
        with httpx.Client(base_url=f"http://127.0.0.1:{gw_port}", timeout=10) as client:
            for label, headers, body, status, code, forwarded in CASES:
                before = httpx.get(mock_calls).json()["calls"]
                resp = client.post("/mcp", content=body, headers=headers)
                moved = httpx.get(mock_calls).json()["calls"] > before
                verdict = judge(resp, status, code, forwarded, moved)
                failures += verdict != "PASS"
                print(f"{verdict:<40} {resp.status_code}  {label}")
        resp = httpx.post(f"http://127.0.0.1:{dead_gw_port}/mcp", content=rpc("tools/list"), headers=VIEWER)
        ok = resp.status_code == 502 and resp.json()["error"]["data"] == {"reason": "unreachable"}
        ok = ok and str(dead_port).encode() not in resp.content
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL':<40} {resp.status_code}  downstream down (no port in body)")
        print(f"\n{len(CASES) + 1 - failures}/{len(CASES) + 1} passed")
        return 1 if failures else 0
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait()


if __name__ == "__main__":
    sys.exit(main())
