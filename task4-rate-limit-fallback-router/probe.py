"""Hostile-input probe against the full ASGI app: `uv run python probe.py`.

Each row builds a fresh on-disk database, drives the real Starlette app through
httpx.ASGITransport with scripted providers, and prints PASS/FAIL. Exit code 1 on any FAIL.
"""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

import httpx

from llm_router.app import create_app
from llm_router.config import Settings
from llm_router.db import connect, init_db
from llm_router.providers import FakeProvider, HttpProvider

KEY = {"X-API-Key": "probe-tenant"}
LEAKS = ("10.0.0.5", "Traceback", "upstream.internal")
results: list[tuple[str, bool, str]] = []
bodies: list[str] = []  # every error body we saw, checked for leaks at the end


class Clock:
    def __init__(self):
        self.ms = 1_700_000_000_000

    def __call__(self):
        return self.ms


async def gateway(primary, secondary=None, clock=None, **overrides):
    tmp = Path(tempfile.mkdtemp(prefix="probe-"))
    settings = Settings(db_path=tmp / "gw.db", **overrides)
    await init_db(settings)
    kwargs = {"now_ms": clock} if clock else {}
    app = create_app(settings, primary, secondary or FakeProvider("s"), **kwargs)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
    return client, settings


async def rows(settings) -> int:
    async with connect(settings) as conn:
        return (await (await conn.execute("SELECT COUNT(*) FROM usage")).fetchone())[0]


async def post(client, body=None, headers=KEY, content=None):
    if content is None:
        resp = await client.post("/v1/completions", json=body, headers=headers)
    else:
        resp = await client.post("/v1/completions", content=content, headers=headers)
    if resp.status_code != 200:
        bodies.append(resp.text)
    return resp


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def etype(resp) -> str | None:
    try:
        return resp.json()["error"]["type"]
    except (ValueError, KeyError, TypeError):
        return None


def body(prompt="abcd", max_tokens=1999):
    return {"prompt": prompt, "max_tokens": max_tokens}  # "abcd" -> 1 + max_tokens tokens


def sized(n: int) -> dict:
    """A body whose estimate is exactly n tokens (max_tokens is capped at 32,000)."""
    return {"prompt": "x" * (4 * (n - 1)), "max_tokens": 1}


# ---------------------------------------------------------------- limiter rows


async def race_last_tokens():
    c, _ = await gateway(FakeProvider("p", tokens=2000))
    async with c:
        rs = await asyncio.gather(*(post(c, body()) for _ in range(50)))
    codes = [r.status_code for r in rs]
    check("race: 50 x 2000 vs 50k -> exactly 25 admitted",
          codes.count(200) == 25 and codes.count(429) == 25, f"200s={codes.count(200)}")


async def race_single_slot():
    c, _ = await gateway(FakeProvider("p", tokens=49_000))
    async with c:
        first = await post(c, sized(49_000))                  # 49,000 used, 1,000 left
        rs = await asyncio.gather(*(post(c, sized(1_000)) for _ in range(10)))
    check("setup: 49,000-token request admitted", first.status_code == 200, str(first.status_code))
    wins = sum(r.status_code == 200 for r in rs)
    check("race: 10 x 1,000 for the last 1,000 tokens -> exactly 1", wins == 1, f"wins={wins}")


async def exact_limit_and_one_over():
    p = FakeProvider("p", tokens=998)
    c, _ = await gateway(p, limit_tokens=1000)
    async with c:
        a = await post(c, body(max_tokens=998))               # est 999 -> 998 used
        p.tokens = 2
        b = await post(c, body(max_tokens=1))                 # 998 + 2 = 1000: at the limit
        d = await post(c, body(max_tokens=1))                 # 1000 + 2: over
    check("limit: used + est == limit is admitted", a.status_code == b.status_code == 200)
    check("limit: one request over is 429 rate_limited",
          d.status_code == 429 and etype(d) == "rate_limited", str(d.status_code))


async def window_edges():
    clock = Clock()
    c, s = await gateway(FakeProvider("p", tokens=50_000), clock=clock)
    async with c:
        await post(c, sized(50_000))
        clock.ms += 59_999
        held = await post(c, body(max_tokens=1))
        clock.ms += 1
        freed = await post(c, body(max_tokens=1))
    check("window: row 59.999 s old still counts", held.status_code == 429, str(held.status_code))
    check("window: row exactly 60 s old is evicted", freed.status_code == 200,
          str(freed.status_code))
    check("window: evicted row is gone from disk", await rows(s) == 1)
    ra = held.headers.get("retry-after", "")
    check("our Retry-After is RFC 9110 delay-seconds >= 1", ra.isdigit() and int(ra) >= 1, ra)


async def retry_after_full_window():
    c, _ = await gateway(FakeProvider("p", tokens=50_000))
    async with c:
        await post(c, sized(50_000))
        r = await post(c, body(max_tokens=1))
    check("our Retry-After right after filling = 60", r.headers.get("retry-after") == "60",
          r.headers.get("retry-after", "missing"))


async def table_bounded():
    clock = Clock()
    c, s = await gateway(FakeProvider("p", tokens=10), clock=clock)
    async with c:
        for i in range(200):                                  # 10 simulated minutes
            await post(c, body(max_tokens=9), headers={"X-API-Key": f"tenant-{i % 50}"})
            clock.ms += 3_000
    n = await rows(s)
    check("table bounded: 200 reqs over 10 min, 50 tenants -> <= 1 window of rows",
          n <= 20, f"rows={n}")


# ---------------------------------------------------------------- fallback rows


async def provider_retry_after_variants():
    for label, headers in [
        ("none", {}),
        ("120", {"Retry-After": "120"}),
        ("HTTP-date", {"Retry-After": "Fri, 31 Dec 1999 23:59:59 GMT"}),
        ("huge", {"Retry-After": "9" * 40}),
        ("garbage", {"Retry-After": "soon"}),
    ]:
        transport = httpx.MockTransport(
            lambda r, h=headers: httpx.Response(429, headers=h, text="slow down 10.0.0.5")
        )
        primary = HttpProvider("p", "http://upstream.internal/v1",
                               httpx.AsyncClient(transport=transport))
        c, _ = await gateway(primary)
        async with c:
            r = await post(c, body(max_tokens=5))
        check(f"provider 429, Retry-After {label} -> secondary, header not surfaced",
              r.status_code == 200 and r.json()["provider"] == "secondary"
              and "retry-after" not in r.headers, str(r.status_code))


async def hang_at_real_deadline():
    p = FakeProvider("p", delay_s=30)
    c, _ = await gateway(p)
    async with c:
        t = time.monotonic()
        r = await post(c, body(max_tokens=5))
        elapsed = time.monotonic() - t
    check("primary hangs -> secondary at ~3.0 s (2.9-3.3)",
          r.status_code == 200 and 2.9 < elapsed < 3.3, f"{elapsed:.2f}s")
    check("hung primary was cancelled", p.cancelled == 1)


async def deadline_edges():
    early = FakeProvider("p", delay_s=0.35)
    c, _ = await gateway(early, upstream_timeout_s=0.5)
    async with c:
        r = await post(c, body(max_tokens=5))
    check("primary at deadline - 150 ms -> primary", r.json().get("provider") == "primary")
    late = FakeProvider("p", delay_s=0.65)
    c, _ = await gateway(late, upstream_timeout_s=0.5)
    async with c:
        r = await post(c, body(max_tokens=5))
    check("primary at deadline + 150 ms -> secondary, primary cancelled",
          r.json().get("provider") == "secondary" and late.cancelled == 1)


async def both_fail_variants():
    c, s = await gateway(FakeProvider("p", "429"), FakeProvider("s", "500"))
    async with c:
        r = await post(c, body(max_tokens=5))
    check("both fail -> 503 upstream_unavailable",
          r.status_code == 503 and etype(r) == "upstream_unavailable")
    check("both fail -> reservation released", await rows(s) == 0)
    c, _ = await gateway(FakeProvider("p", delay_s=9), FakeProvider("s", delay_s=9),
                         upstream_timeout_s=0.3)
    async with c:
        t = time.monotonic()
        r = await post(c, body(max_tokens=5))
        elapsed = time.monotonic() - t
    check("both hang -> 503 after ~2 x timeout", r.status_code == 503 and elapsed < 0.8,
          f"{elapsed:.2f}s")


async def status_routing():
    for mode, want_status, want in [("400", 400, "secondary not called"),
                                    ("500", 200, "secondary"), ("conn", 200, "secondary")]:
        s = FakeProvider("s")
        c, _ = await gateway(FakeProvider("p", mode), s)
        async with c:
            r = await post(c, body(max_tokens=5))
        ok = r.status_code == want_status and (s.calls == 0 if mode == "400" else s.calls == 1)
        check(f"primary {mode} -> {want_status} ({want})", ok, str(r.status_code))
    c, _ = await gateway(FakeProvider("p", "boom"))
    async with c:
        r = await post(c, body(max_tokens=5))
    check("unknown provider exception -> 500 internal", r.status_code == 500
          and etype(r) == "internal")


# ---------------------------------------------------------------- boundary rows


async def hostile_requests():
    c, _ = await gateway(FakeProvider("p"))
    async with c:
        cases = [
            ("missing X-API-Key -> 401", await post(c, body(), headers={}), 401),
            ("whitespace X-API-Key -> 401", await post(c, body(), headers={"X-API-Key": " "}), 401),
            ("64 KB X-API-Key -> 200 (hashed)",
             await post(c, body(max_tokens=5), headers={"X-API-Key": "k" * 65_536}), 200),
            ("malformed JSON -> 400", await post(c, content=b'{"prompt": '), 400),
            ("non-UTF-8 body -> 400", await post(c, content=b"\xff\xfe\x00"), 400),
            ("max_tokens as string -> 400", await post(c, {"prompt": "x", "max_tokens": "5"}), 400),
            ("unknown field -> 400", await post(c, {"prompt": "x", "model": "gpt"}), 400),
            ("est > whole limit -> 400 not 429",
             await post(c, {"prompt": "x" * 100_000, "max_tokens": 32_000}), 400),
            ("NUL + emoji prompt -> 200", await post(c, {"prompt": "\x00🔥", "max_tokens": 5}), 200),
        ]
        not_found = await c.get("/admin")
        bodies.append(not_found.text)
    for name, resp, want in cases:
        check(name, resp.status_code == want, str(resp.status_code))
    check("unknown path -> 404 in the gateway payload",
          not_found.status_code == 404 and etype(not_found) == "invalid_request")


async def client_cancel():
    p = FakeProvider("p", delay_s=30)
    c, s = await gateway(p, upstream_timeout_s=10)
    async with c:
        task = asyncio.create_task(post(c, body(max_tokens=5)))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    check("client cancel -> primary cancelled, reservation released",
          p.cancelled == 1 and await rows(s) == 0, f"cancelled={p.cancelled} rows={await rows(s)}")


async def hold_write_lock(db: Path, seconds: float) -> asyncio.subprocess.Process:
    code = (
        "import sqlite3, sys, time\n"
        "c = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "c.execute('BEGIN IMMEDIATE'); print('locked', flush=True)\n"
        "time.sleep(float(sys.argv[2])); c.execute('COMMIT')\n"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(db), str(seconds), stdout=asyncio.subprocess.PIPE
    )
    await proc.stdout.readline()  # wait until the other process really holds the lock
    return proc


async def sqlite_busy():
    c, s = await gateway(FakeProvider("p"))
    async with c:
        proc = await hold_write_lock(s.db_path, 0.5)
        t = time.monotonic()
        r = await post(c, body(max_tokens=5))
        waited = time.monotonic() - t
        await proc.wait()
    check("other process holds write lock 0.5 s -> request waits, then 200",
          r.status_code == 200 and waited >= 0.3, f"{r.status_code} after {waited:.2f}s")
    c, s = await gateway(FakeProvider("p"), busy_timeout_ms=100)
    async with c:
        proc = await hold_write_lock(s.db_path, 1.0)
        r = await post(c, body(max_tokens=5))
        await proc.wait()
    check("write lock held past busy_timeout -> 500 internal, no detail",
          r.status_code == 500 and etype(r) == "internal", str(r.status_code))


async def main() -> int:
    for row in (race_last_tokens, race_single_slot, exact_limit_and_one_over, window_edges,
                retry_after_full_window, table_bounded, provider_retry_after_variants,
                hang_at_real_deadline, deadline_edges, both_fail_variants, status_routing,
                hostile_requests, client_cancel, sqlite_busy):
        try:
            await row()
        except Exception as exc:  # noqa: BLE001 - a crashing row is a FAIL, not a probe crash
            check(f"{row.__name__} (crashed)", False, f"{type(exc).__name__}: {exc}")
    leaked = [b for b in bodies if any(word in b for word in LEAKS)]
    check(f"no error body ({len(bodies)} seen) contains a hostname or traceback", not leaked,
          leaked[0][:80] if leaked else "")
    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
