from __future__ import annotations

import json

from shared.http.base import HttpModel


class ErrorResponse(HttpModel):
    """Standard error body returned by all non-2xx JSON responses."""

    detail: str
    code: str = ""


class HttpApiError(RuntimeError):
    """HTTP API request failed with a non-success status."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error: ErrorResponse | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error

    @property
    def detail(self) -> str | None:
        return self.error.detail if self.error is not None else None

    @property
    def code(self) -> str:
        return self.error.code if self.error is not None else ""


def http_api_error_from_body(
    status_code: int,
    raw: str,
    *,
    fallback: str | None = None,
) -> HttpApiError:
    if raw:
        try:
            error = ErrorResponse.model_validate(json.loads(raw))
            return HttpApiError(error.detail, status_code=status_code, error=error)
        except (json.JSONDecodeError, ValueError):
            pass
    message = raw or fallback or f"HTTP {status_code}"
    return HttpApiError(message, status_code=status_code, error=None)


__all__ = [
    "ErrorResponse",
    "HttpApiError",
    "http_api_error_from_body",
]
