"""One pooled HTTP client for every internal hop."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import IO
from urllib.parse import urlparse

import httpx

DEFAULT_INTERNAL_HTTP_TIMEOUT_SECONDS = 30.0


class InternalHttpError(RuntimeError):
    """An internal hop could not be completed."""


@dataclass(slots=True)
class InternalHttpClient:
    """The single pooled client for platform-internal HTTP."""

    timeout_seconds: float = DEFAULT_INTERNAL_HTTP_TIMEOUT_SECONDS
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=False,
            )
        return self._client

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterable[bytes] | IO[bytes] | None = None,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        timeout = timeout_seconds or self.timeout_seconds
        merged = dict(headers or {})
        try:
            return self._ensure_client().request(
                method,
                url,
                headers=merged,
                content=content,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise InternalHttpError(f"{method} {_safe_target(url)} failed: {exc}") from exc

    @contextmanager
    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterable[bytes] | IO[bytes] | None = None,
        timeout_seconds: float | None = None,
    ) -> Iterator[httpx.Response]:
        timeout = timeout_seconds or self.timeout_seconds
        merged = dict(headers or {})
        client = self._ensure_client()
        opened = client.stream(method, url, headers=merged, content=content, timeout=timeout)
        try:
            response = opened.__enter__()
        except httpx.HTTPError as exc:
            raise InternalHttpError(f"{method} {_safe_target(url)} failed: {exc}") from exc
        try:
            yield response
        except httpx.HTTPError as exc:
            raise InternalHttpError(f"{method} {_safe_target(url)} failed: {exc}") from exc
        finally:
            opened.__exit__(None, None, None)

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            client.close()


def _safe_target(url: str) -> str:
    """A URL rendered for an error message, without credentials or query."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{parsed.path}"


__all__ = [
    "DEFAULT_INTERNAL_HTTP_TIMEOUT_SECONDS",
    "InternalHttpClient",
    "InternalHttpError",
]
