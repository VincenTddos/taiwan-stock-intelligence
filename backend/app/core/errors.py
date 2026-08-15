"""Error model — RFC 9457 Problem Details.

Rationale (API_SPEC.md §1.3): a single machine-readable error shape means the
frontend never has to guess whether a failure is retryable, and every error
carries the `request_id` needed to find the matching log line.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, request_id_var

log = get_logger(__name__)

ERROR_BASE = "https://twquant.local/errors"


class AppError(Exception):
    """Base for all deliberately-raised application errors."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type: str = "internal-error"
    title: str = "Internal server error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.detail = detail or self.title
        self.errors = errors or []
        self.headers = headers or {}
        super().__init__(self.detail)

    def to_problem(self, instance: str) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": f"{ERROR_BASE}/{self.error_type}",
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            "instance": instance,
            "request_id": request_id_var.get(),
        }
        if self.errors:
            problem["errors"] = self.errors
        return problem


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "not-found"
    title = "Resource not found"


class ValidationError(AppError):
    status_code = 422
    error_type = "validation-error"
    title = "Validation failed"


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "authentication-failed"
    title = "Authentication failed"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail, headers={"WWW-Authenticate": "Bearer"})


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    error_type = "permission-denied"
    title = "Permission denied"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "conflict"
    title = "Conflict"


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "rate-limited"
    title = "Too many requests"


class DataNotAvailableError(AppError):
    """No data — and we say so. We never substitute fabricated values."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "data-not-available"
    title = "Data not available"


class LicenseRestrictedError(AppError):
    status_code = 451
    error_type = "license-restricted"
    title = "Data unavailable due to licensing"


class UpstreamUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_type = "upstream-unavailable"
    title = "Upstream data provider unavailable"


def _problem_response(problem: dict[str, Any], headers: dict[str, str]) -> JSONResponse:
    return JSONResponse(
        status_code=int(problem["status"]),
        content=problem,
        headers=headers,
        media_type="application/problem+json",
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        log.warning(
            "app_error",
            error_type=exc.error_type,
            status=exc.status_code,
            detail=exc.detail,
            path=request.url.path,
        )
        return _problem_response(exc.to_problem(request.url.path), exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(x) for x in e.get("loc", [])), "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        err = ValidationError("Request validation failed", errors=errors)
        return _problem_response(err.to_problem(request.url.path), {})

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        err = AppError(str(exc.detail))
        err.status_code = exc.status_code
        err.error_type = "http-error"
        err.title = str(exc.detail)
        return _problem_response(err.to_problem(request.url.path), dict(exc.headers or {}))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the full traceback, return an opaque message: internals are not
        # part of the public API surface.
        log.exception("unhandled_exception", path=request.url.path, error=str(exc))
        return _problem_response(AppError().to_problem(request.url.path), {})
