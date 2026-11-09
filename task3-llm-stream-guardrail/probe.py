"""Hostile-input probe over a live uvicorn server and a real TCP client.

Run: uv run python probe.py     (prints one PASS/FAIL line per case; exit 1 on any FAIL)
Unit tests use in-process transports; this checks the same behaviour over real sockets.
"""

import itertools
import json
import sys
import time

import anyio
import httpx
import uvicorn

from guardrail.app import create_app
from guardrail.provider import STALL, MockUpstream, script, sse_chunk

URL = "http://mock-upstream/v1/chat/completions"
R = "[REDACTED]"


def split_bytes(frame: bytes, *cuts: int) -> list[bytes]:
    edges = [0, *cuts, len(frame)]
    return [frame[a:b] for a, b in itertools.pairwise(edges)]


# Raw UTF-8 on the wire (json.dumps would escape it to ASCII), cut inside both multi-byte chars.
UTF8 = b"data: " + '{"choices":[{"delta":{"content":"caf\u00e9 \U0001f642 ok"}}]}'.encode() + b"\n\n"
UTF8_CUTS = (UTF8.index(b"\xc3") + 1, UTF8.index(b"\xf0") + 2)
DONE = b"data: [DONE]\n\n"

# name -> (upstream steps, expected client content or None, expected final event)
CASES = {
    "email split at @": (script("mail john", "@example.com ok"), f"mail {R} ok", "done"),
    "email split in TLD": (script("mail john@example.c", "om ok"), f"mail {R} ok", "done"),
    "email in 3 chunks": (script("a jo", "hn.doe@exa", "mple.co.uk b"), f"a {R} b", "done"),
    "SSN split after 123-": (script("ssn 123-", "45-6789."), f"ssn {R}.", "done"),
    "card split every 4": (script("c ", "4111", " 111", "1 11", "11 1", "111", " end"), f"c {R} end", "done"),
    "Amex split": (script("amex 3782 8224", "63 10005!"), f"amex {R}!", "done"),
    "card glued to word": (script("Visa41111111", "11111111 x"), f"Visa{R} x", "done"),
    "bad Luhn untouched": (script("n 4111 1111 1111 1112"), "n 4111 1111 1111 1112", "done"),
    "phone untouched": (script("call 512-555-", "0199"), "call 512-555-0199", "done"),
    "fullwidth SSN": (script("\uff11\uff12\uff13-\uff14\uff15-", "\uff16\uff17\uff18\uff19"), R, "done"),
    "UTF-8 cut mid-char": ([*split_bytes(UTF8, *UTF8_CUTS), DONE], "caf\u00e9 \U0001f642 ok", "done"),
    "frame cut mid-line": ([*split_bytes(sse_chunk("joe@example.com"), 9, 20), DONE], R, "done"),
    "zero-width space in email": (script("x john\u200b@exa", "mple.com"), f"x {R}", "done"),
    "soft hyphen in SSN": (script("123-4\u00ad5-6789"), R, "done"),
    "word joiner in card": (script("4111\u2060 1111 1111 1111"), R, "done"),
    "ZWJ emoji kept": (script("\U0001f468\u200d\U0001f469 hi"), "\U0001f468\u200d\U0001f469 hi", "done"),
    "keep-alives and empty events": ([b": ping\n\n", b"\n\n", *script("hi")], "hi", "done"),
    "multi-line data event": ([b'data: {"choices":\ndata: [{"delta":{"content":"hi"}}]}\n\n', DONE],
                              "hi", "done"),
    "CRLF line endings": ([sse_chunk("crlf").replace(b"\n", b"\r\n"), DONE.replace(b"\n", b"\r\n")],
                          "crlf", "done"),
    "held tail flushed before DONE": (script("end 4111 1111 1111 1111"), f"end {R}", "done"),
    # On failure the held tail is dropped (fail closed): here that is only the trailing space.
    "malformed JSON": ([sse_chunk("ok "), b"data: {nope\n\n"], "ok", "error:upstream_protocol_error"),
    "non-string content": ([b'data: {"choices":[{"delta":{"content":7}}]}\n\n'], "",
                           "error:upstream_protocol_error"),
    "no [DONE]": (script("john@exa", done=False), "", "error:upstream_truncated"),
    "stall mid-stream": ([sse_chunk("ok then jo"), STALL], "ok then ", "error:upstream_timeout"),
    "upstream reset mid-stream": ([sse_chunk("a "), httpx.ReadError("reset by 10.1.2.3")], "a",
                                  "error:upstream_error"),
    "huge single event (1 MB)": (script("a" * 1_000_000), "a" * 1_000_000, "done"),
}


def parse(body: bytes) -> tuple[str, list[str]]:
    text, kinds = "", []
    for frame in body.decode().split("\n\n"):
        if frame.startswith("event: error"):
            kinds.append("error:" + json.loads(frame.split("data: ", 1)[1])["error"]["code"])
        elif frame == "data: [DONE]":
            kinds.append("done")
        elif frame:
            text += json.loads(frame[6:])["choices"][0]["delta"]["content"]
    return text, kinds


def request(model: str, stream: bool = True) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream}


async def run(client: httpx.AsyncClient, upstream: MockUpstream) -> list[tuple[str, bool, str]]:
    results = []
    for name, (_, want_text, want_end) in CASES.items():
        resp = await client.post("/v1/chat/completions", json=request(name))
        text, kinds = parse(resp.content)
        ok = resp.status_code == 200 and text == want_text and kinds[-1:] == [want_end]
        ok = ok and kinds.count("done") <= 1 and b"10.1.2.3" not in resp.content
        results.append((name, ok, f"end={kinds[-1:]} text={text[:60]!r}"))

    resp = await client.post("/v1/chat/completions", json=request("upstream 500"))
    ok = resp.status_code == 502 and b"db-7" not in resp.content
    results.append(("upstream 500 -> sanitized 502", ok, resp.text))

    resp = await client.post("/v1/chat/completions", json=request("x", stream=False))
    results.append(("stream:false -> 400", resp.status_code == 400, resp.text))

    async with client.stream("POST", "/v1/chat/completions", json=request("long")) as r:
        async for _ in r.aiter_raw():
            break  # read one chunk, then hang up
    for _ in range(100):
        if upstream.last.closed:
            break
        await anyio.sleep(0.01)
    sent = upstream.last.sent
    results.append(("client disconnect closes upstream", upstream.last.closed and sent < 500, f"sent={sent}"))

    t0, first = time.monotonic(), None
    async with client.stream("POST", "/v1/chat/completions", json=request("ttft")) as r:
        async for _ in r.aiter_raw():
            first = first or time.monotonic() - t0
    results.append(("TTFT", first is not None and first < 0.1, f"first byte {first * 1000:.0f} ms, 20 ms/token"))
    return results


async def main() -> int:
    scenarios = {name: (200, steps) for name, (steps, _, _) in CASES.items()}
    scenarios["demo"] = scenarios["email split at @"]
    scenarios["upstream 500"] = (500, [b"Traceback at db-7.internal:5432"])
    scenarios["long"] = (200, script(*[f"w{i} " for i in range(500)]))
    scenarios["ttft"] = (200, script(*[f"tok{i} " for i in range(20)]))
    upstream = MockUpstream(scenarios, delay=0.02)
    app = create_app(httpx.AsyncClient(transport=upstream), URL, stall_s=0.5)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve)
        while not server.started:
            await anyio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            results = await run(client, upstream)
        server.should_exit = True
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:36} {detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
