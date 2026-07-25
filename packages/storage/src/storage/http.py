from __future__ import annotations

import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from types import ModuleType, TracebackType
from typing import Protocol, TypeGuard, runtime_checkable
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class StorageHttpResponse:
    status: int
    body: bytes


class _ReadableResponse(Protocol):
    def read(self) -> bytes: ...


class _ResponseContext(Protocol):
    def __enter__(self) -> _ReadableResponse: ...

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


@runtime_checkable
class _StatusResponse(Protocol):
    status: int


class _UrlOpenFactory(Protocol):
    def urlopen(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _ResponseContext: ...


def request_storage_http(
    url: str,
    *,
    method: str,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout_seconds: float,
) -> StorageHttpResponse:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("storage HTTP URL must be an HTTP(S) origin without credentials")
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers or {}),
        method=method,
    )
    request_module: ModuleType = urllib.request
    if not _is_urlopen_factory(request_module):
        raise RuntimeError("urllib.request is missing the URL open operation")
    with request_module.urlopen(request, timeout=timeout_seconds) as response:
        status = response.status if isinstance(response, _StatusResponse) else 200
        return StorageHttpResponse(status=status, body=response.read())


def _is_urlopen_factory(value: ModuleType) -> TypeGuard[_UrlOpenFactory]:
    return callable(getattr(value, "urlopen", None))


__all__ = ["StorageHttpResponse", "request_storage_http"]
