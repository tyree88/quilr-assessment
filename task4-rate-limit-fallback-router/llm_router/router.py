import asyncio
import logging

from llm_router.errors import ErrorType, GatewayError
from llm_router.models import CompletionRequest
from llm_router.providers import Provider, ProviderError, ProviderResponse

log = logging.getLogger("llm_router")


class _FailOver(Exception):
    """This provider cannot serve now; try the next one."""


class ModelRouter:
    def __init__(self, primary: Provider, secondary: Provider, timeout_s: float):
        self._providers = (("primary", primary), ("secondary", secondary))
        self._timeout_s = timeout_s

    async def complete(
        self, req: CompletionRequest, request_id: str
    ) -> tuple[ProviderResponse, str]:
        """Primary first; on 429, timeout, 5xx or connection error, the secondary. Each
        provider is called at most once."""
        for role, provider in self._providers:
            try:
                return await self._attempt(provider, req, request_id), role
            except _FailOver:
                continue
        raise GatewayError(ErrorType.UPSTREAM_UNAVAILABLE)

    async def _attempt(
        self, provider: Provider, req: CompletionRequest, request_id: str
    ) -> ProviderResponse:
        try:
            # The deadline covers connect + the full response. On expiry asyncio cancels
            # the awaited provider call in place (same task, nothing left running) and
            # re-raises it here as TimeoutError. A cancel from outside (client gone) is
            # not converted and propagates as CancelledError.
            async with asyncio.timeout(self._timeout_s):
                return await provider.complete(req)
        except TimeoutError:
            log.warning("request_id=%s provider=%s timeout after %.1fs",
                        request_id, provider.name, self._timeout_s)
            raise _FailOver from None
        except ProviderError as exc:
            log.warning("request_id=%s provider=%s status=%s detail=%r",
                        request_id, provider.name, exc.status, exc.detail)
            if exc.status is None or exc.status == 429 or exc.status >= 500:
                raise _FailOver from None
            raise GatewayError(ErrorType.INVALID_REQUEST) from None
