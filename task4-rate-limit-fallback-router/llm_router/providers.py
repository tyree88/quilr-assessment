import asyncio
from dataclasses import dataclass
from typing import Annotated, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from llm_router.models import CompletionRequest


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str
    total_tokens: int


class ProviderError(Exception):
    """A provider failed. `status` drives routing; `detail` is for the server log only."""

    def __init__(self, status: int | None, detail: str):
        super().__init__(detail)
        self.status = status  # None = no usable HTTP response (connection, decoding)
        self.detail = detail


class Provider(Protocol):
    name: str

    async def complete(self, req: CompletionRequest) -> ProviderResponse: ...


class _UpstreamUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    total_tokens: Annotated[int, Field(ge=0)]


class _UpstreamBody(BaseModel):
    model_config = ConfigDict(extra="ignore")  # providers add fields; we read only these
    text: str
    usage: _UpstreamUsage


class HttpProvider:
    """POSTs the request as JSON. The deadline is the router's asyncio.timeout, not httpx."""

    def __init__(self, name: str, url: str, client: httpx.AsyncClient):
        self.name = name
        self._url = url
        self._client = client

    async def complete(self, req: CompletionRequest) -> ProviderResponse:
        try:
            resp = await self._client.post(self._url, json=req.model_dump())
        except httpx.HTTPError as exc:  # transport, decoding, redirects: no usable response
            raise ProviderError(None, f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            # Retry-After from the provider is deliberately not parsed (decision 10).
            raise ProviderError(resp.status_code, resp.text[:500])
        try:
            body = _UpstreamBody.model_validate_json(resp.content)
        except ValidationError as exc:
            raise ProviderError(502, f"malformed upstream body: {exc}") from exc
        return ProviderResponse(text=body.text, total_tokens=body.usage.total_tokens)


class FakeProvider:
    """Scripted provider for tests, the probe and the local demo.

    mode: ok | 429 | 500 | 400 | conn | boom. `delay_s` sleeps first (a hang when large).
    Records calls and cancellations so tests can prove the timeout cancelled it.
    """

    def __init__(self, name: str, mode: str = "ok", *, delay_s: float = 0.0, tokens: int = 42):
        self.name = name
        self.mode = mode
        self.delay_s = delay_s
        self.tokens = tokens
        self.calls = 0
        self.cancelled = 0

    async def complete(self, req: CompletionRequest) -> ProviderResponse:
        self.calls += 1
        try:
            await asyncio.sleep(self.delay_s)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        leak = "Traceback (most recent call last): upstream host 10.0.0.5:8443"
        match self.mode:
            case "ok":
                return ProviderResponse(text=f"{self.name} says hi", total_tokens=self.tokens)
            case "429" | "500" | "400":
                raise ProviderError(int(self.mode), leak)
            case "conn":
                raise ProviderError(None, leak)
            case _:
                raise RuntimeError(leak)
