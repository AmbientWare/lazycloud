from __future__ import annotations

import json
import urllib.error
import urllib.parse

from shared.http.base import HttpModel


class ErrorResponse(HttpModel):
    """Standard error body returned by all non-2xx JSON responses."""

    detail: str


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


class HttpTransportError(RuntimeError):
    """An HTTP request failed before a response was received."""

    def __init__(self, method: str, url: str, reason: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        display_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        super().__init__(f"{method.upper()} {display_url}: {reason}")
        self.method = method.upper()
        self.url = url
        self.reason = reason


class HttpResponseDecodeError(RuntimeError):
    """An HTTP response body does not match the channel's JSON protocol."""


def http_api_error_from_http_error(exc: urllib.error.HTTPError) -> HttpApiError:
    raw = exc.read().decode("utf-8", errors="replace")
    return http_api_error_from_body(exc.code, raw, fallback=str(exc))


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
    "HttpResponseDecodeError",
    "HttpTransportError",
    "http_api_error_from_body",
    "http_api_error_from_http_error",
]
