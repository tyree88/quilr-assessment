"""Stdout must carry only JSON-RPC. These tests run the real server as a subprocess over pipes.

Keep stdin open until each reply arrives: at stdin EOF the server exits
without answering requests that are still in flight.
"""

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "mcp_server.py"
TIMEOUT_S = 10
VALID = {"customer_id": "CUST-00001", "amount": 40.0, "reason": "Damaged on arrival"}


def _start():
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump():
        # A thread reads stdout so a missing reply fails with a timeout, not a hang.
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)  # EOF

    threading.Thread(target=pump, daemon=True).start()
    return proc, lines


def _parse(line: str) -> dict:
    msg = json.loads(line)  # a stray print() or log line on stdout fails here
    assert msg.get("jsonrpc") == "2.0", f"not JSON-RPC 2.0: {line!r}"
    return msg


def _send(proc, msg: dict) -> None:
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def _request(proc, lines, msg_id: int, method: str, params: dict | None = None) -> dict:
    _send(proc, {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
    while True:
        line = lines.get(timeout=TIMEOUT_S)
        assert line is not None, f"server closed stdout before replying to id {msg_id}"
        msg = _parse(line)
        if msg.get("id") == msg_id:
            return msg


def test_no_stdout_before_client_speaks():
    done = subprocess.run(
        [sys.executable, str(SERVER)], stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False
    )
    assert done.returncode == 0
    assert done.stdout == b""


def test_every_stdout_line_is_jsonrpc():
    proc, lines = _start()
    try:
        init = _request(
            proc,
            lines,
            1,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
        )
        assert init["result"]["serverInfo"] == {"name": "customer-ops", "version": "0.1.0"}
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools = _request(proc, lines, 2, "tools/list")["result"]["tools"]
        assert {t["name"] for t in tools} == {"get_customer_record", "trigger_refund"}

        malformed = _request(
            proc, lines, 3, "tools/call", {"name": "get_customer_record", "arguments": {"customer_id": "cust-00001"}}
        )
        assert malformed["error"]["code"] == -32602

        null_args = _request(proc, lines, 4, "tools/call", {"name": "get_customer_record", "arguments": None})
        assert null_args["error"]["code"] == -32602

        refused = _request(
            proc, lines, 5, "tools/call", {"name": "trigger_refund", "arguments": {**VALID, "customer_id": "CUST-00003"}}
        )
        assert refused["result"]["isError"] is True

        ok = _request(proc, lines, 6, "tools/call", {"name": "trigger_refund", "arguments": VALID})
        assert not ok["result"].get("isError")
        assert ok["result"]["structuredContent"]["remaining_balance"] == 210.0
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise

    # Anything the server wrote after the last reply must also be JSON-RPC.
    while (line := lines.get(timeout=TIMEOUT_S)) is not None:
        _parse(line)
    assert proc.returncode == 0
