"""Mock LLM upstream: a real httpx transport that streams scripted SSE bytes.

The gateway talks to it through httpx exactly as it would to a real provider, so tests and
the probe exercise the real client code path (status check, aiter_lines, aclose).
"""

import json

import anyio
import httpx

STALL = object()  # script step: stop sending forever (tests the read timeout)


def sse_chunk(text: str) -> bytes:
    """One OpenAI-style chunk frame carrying `text` as the delta content."""
    body = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": text}}]}
    return f"data: {json.dumps(body)}\n\n".encode()


def script(*parts: str, done: bool = True) -> list:
    """Frames for each text part, then [DONE]."""
    return [sse_chunk(p) for p in parts] + ([b"data: [DONE]\n\n"] if done else [])


class ScriptStream(httpx.AsyncByteStream):
    """Steps are bytes (sent as-is), an exception (raised), or STALL."""

    def __init__(self, steps: list, delay: float) -> None:
        self._steps, self._delay = steps, delay
        self.closed = False
        self.sent = 0

    async def __aiter__(self):
        for step in self._steps:
            # Yield to the loop like a socket read does. Without it a delay-0 stream runs in
            # one task step and the loop never purges cancelled timeout timers (memory grows).
            await anyio.sleep(self._delay)
            if step is STALL:
                await anyio.sleep_forever()
            if isinstance(step, Exception):
                raise step
            self.sent += 1
            yield step

    async def aclose(self) -> None:
        self.closed = True  # proof the gateway released the upstream


class MockUpstream(httpx.MockTransport):
    """Picks a scenario by the request's "model" field. Scenario = (status, steps)."""

    def __init__(self, scenarios: dict[str, tuple[int, list]], delay: float = 0.0) -> None:
        self.scenarios, self.delay = scenarios, delay
        self.last: ScriptStream | None = None
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content).get("model", "demo")
        status, steps = self.scenarios.get(model, self.scenarios["demo"])
        self.last = ScriptStream(steps, self.delay)
        return httpx.Response(status, headers={"content-type": "text/event-stream"}, stream=self.last)


DEMO = {
    "demo": (200, script(
        "Sure. Reach me at jo", "hn.doe@exa", "mple.com or call 512-555-0199. ",
        "My SSN is 123-", "45-6789 and my card is 4111 1111 ", "1111 1111. ",
        "This one fails Luhn: 4111 1111 1111 1112.",
    )),
}
