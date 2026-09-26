import logging
from time import perf_counter
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .config import Settings
from .request_context import (
    reset_request_id,
    set_business_context,
    set_request_id,
    set_trace_context,
)

logger = logging.getLogger(__name__)


def _response(
    request: Request, status: int, code: str, message: str, details: Any = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": getattr(request.state, "request_id", "-"),
                "details": details,
            }
        },
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID", "").strip() or str(uuid4())
        request.state.request_id = request_id[:128]
        token = set_request_id(request.state.request_id)
        parts = request.headers.get("traceparent", "").split("-")
        trace_id = parts[1] if len(parts) > 1 and len(parts[1]) == 32 else uuid4().hex
        span_id = uuid4().hex[:16]
        set_trace_context(trace_id, span_id)
        started = perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers["traceparent"] = f"00-{trace_id}-{span_id}-01"
            logger.info(
                "request.completed",
                extra={"duration_ms": round((perf_counter() - started) * 1000, 3)},
            )
            return response
        finally:
            reset_request_id(token)
            set_business_context()
            set_trace_context("-", "-")


def install_error_handling(app: FastAPI, settings: Settings) -> None:
    app.add_middleware(RequestContextMiddleware)

    async def http_error(
        request: Request, error: HTTPException | StarletteHTTPException
    ) -> JSONResponse:
        detail = error.detail
        return _response(
            request,
            error.status_code,
            f"http_{error.status_code}",
            detail if isinstance(detail, str) else "Request failed",
            None if isinstance(detail, str) else detail,
        )

    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return _response(
            request, 422, "validation_error", "Request validation failed", error.errors()
        )

    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        logger.exception("Unhandled request error", exc_info=error)
        return _response(
            request,
            500,
            "internal_error",
            "Internal server error" if settings.production else str(error),
        )

    app.add_exception_handler(HTTPException, cast(Any, http_error))
    app.add_exception_handler(StarletteHTTPException, cast(Any, http_error))
    app.add_exception_handler(RequestValidationError, cast(Any, validation_error))
    app.add_exception_handler(Exception, cast(Any, unexpected_error))
