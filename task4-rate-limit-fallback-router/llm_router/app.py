import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from llm_router.config import Settings
from llm_router.db import init_db
from llm_router.errors import ErrorType, GatewayError, to_response
from llm_router.models import CompletionRequest, CompletionResponse, Usage
from llm_router.providers import Provider
from llm_router.ratelimit import SlidingWindowLimiter, wall_clock_ms
from llm_router.router import ModelRouter

log = logging.getLogger("llm_router")


def create_app(
    settings: Settings,
    primary: Provider,
    secondary: Provider,
    now_ms: Callable[[], int] = wall_clock_ms,
) -> Starlette:
    limiter = SlidingWindowLimiter(settings, now_ms)
    router = ModelRouter(primary, secondary, settings.upstream_timeout_s)

    async def completions(request: Request) -> Response:
        # The trust boundary: every outcome leaves through to_response() or the success
        # path below. Real errors are logged under request_id; the client gets only the id.
        request_id = uuid.uuid4().hex
        try:
            return await _complete(request, request_id)
        except GatewayError as err:
            log.info("request_id=%s rejected type=%s status=%s", request_id, err.type, err.status)
            return to_response(err, request_id)
        except Exception:
            log.exception("request_id=%s unhandled error", request_id)
            return to_response(GatewayError(ErrorType.INTERNAL), request_id)

    async def _complete(request: Request, request_id: str) -> Response:
        key = request.headers.get("x-api-key", "")
        if not key.strip():
            raise GatewayError(ErrorType.INVALID_REQUEST, status=401)
        tenant = hashlib.sha256(key.encode()).hexdigest()  # the raw key never hits disk
        try:
            req = CompletionRequest.model_validate_json(await request.body())
        except ValidationError:
            raise GatewayError(ErrorType.INVALID_REQUEST) from None

        reservation = await limiter.reserve(tenant, req.estimated_tokens())
        try:
            result, role = await router.complete(req, request_id)
        except BaseException:
            # Failed or cancelled: give the quota back. shield() lets the DELETE finish
            # even if this task is cancelled again while it runs.
            await asyncio.shield(limiter.release(reservation))
            raise
        await limiter.reconcile(reservation, result.total_tokens)
        body = CompletionResponse(
            text=result.text, provider=role, usage=Usage(total_tokens=result.total_tokens)
        )
        return JSONResponse(body.model_dump(), headers={"X-Request-ID": request_id})

    async def http_error(request: Request, exc: Exception) -> Response:
        # 404 / 405 from Starlette's router get the same payload shape.
        assert isinstance(exc, HTTPException)
        err = GatewayError(ErrorType.INVALID_REQUEST, status=exc.status_code)
        return to_response(err, uuid.uuid4().hex)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await init_db(settings)
        yield

    return Starlette(
        routes=[Route("/v1/completions", completions, methods=["POST"])],
        exception_handlers={HTTPException: http_error},
        lifespan=lifespan,
    )
