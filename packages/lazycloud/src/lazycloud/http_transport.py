from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from email.message import Message
from typing import Protocol, runtime_checkable

from shared.http.errors import HttpTransportError
from shared.http_transport import build_http_ssl_context
from shared.serialization import to_json_value


class ResponseHeaders(Mapping[str, list[str]]):
    """HTTP response headers with case-insensitive lookup."""

    def __init__(self, values: Mapping[str, Iterable[str]] | None = None) -> None:
        self._values: dict[str, list[str]] = {}
        for name, entries in (values or {}).items():
            self._values[name.lower()] = list(entries)

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getitem__(self, name: str) -> list[str]:
        return self._values[name.lower()]


@dataclass(frozen=True, slots=True)
class RawHttpResponse:
    status_code: int
    headers: ResponseHeaders
    content: bytes
    final_url: str


_SSL_CONTEXT = build_http_ssl_context()


@runtime_checkable
class QueryMapping(Protocol):
    def items(self) -> Iterable[tuple[str, object]]: ...


@runtime_checkable
class QuerySequence(Protocol):
    def __iter__(self) -> Iterator[object]: ...


@runtime_checkable
class UrllibResponse(Protocol):
    status: int
    headers: Message

    def __enter__(self) -> UrllibResponse: ...

    def __exit__(self, *args: object) -> None: ...

    def read(self) -> bytes: ...

    def geturl(self) -> str: ...


@runtime_checkable
class ReadableBody(Protocol):
    def read(self, size: int = -1) -> bytes: ...


def request_raw(
    base_url: str,
    *,
    method: str,
    path: str = "",
    json_body: object | None = None,
    data: bytes | str | ReadableBody | None = None,
    headers: Mapping[str, str] | None = None,
    params: QueryMapping | Iterable[tuple[str, object]] | None = None,
    token: str | None = None,
    timeout_seconds: float = 10.0,
    ssl_context: ssl.SSLContext | None = None,
) -> RawHttpResponse:
    body, content_type = _request_body(json_body=json_body, data=data)
    request_headers = dict(headers or {})
    if not _has_header(request_headers, "accept"):
        request_headers["Accept"] = "*/*"
    if token and not _has_header(request_headers, "authorization"):
        request_headers["Authorization"] = f"Bearer {token}"
    if content_type and not _has_header(request_headers, "content-type"):
        request_headers["Content-Type"] = content_type
    url = _request_url(base_url, path=path, params=params)
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method.upper(),
    )
    try:
        with _urlopen_response(
            request,
            timeout_seconds=timeout_seconds,
            ssl_context=ssl_context or _SSL_CONTEXT,
        ) as response:
            return RawHttpResponse(
                status_code=response.status,
                headers=_response_headers(response.headers),
                content=response.read(),
                final_url=response.geturl(),
            )
    except urllib.error.HTTPError as exc:
        return RawHttpResponse(
            status_code=exc.code,
            headers=_response_headers(exc.headers),
            content=exc.read(),
            final_url=exc.geturl(),
        )
    except (OSError, http.client.HTTPException) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        raise HttpTransportError(method, url, str(reason)) from exc


def _request_body(
    *,
    json_body: object | None,
    data: bytes | str | ReadableBody | None,
) -> tuple[bytes | ReadableBody | None, str]:
    if json_body is not None and data is not None:
        msg = "request accepts either json or data, not both"
        raise ValueError(msg)
    if json_body is not None:
        try:
            encoded = json.dumps(to_json_value(json_body)).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("request json body is not serializable") from exc
        return encoded, "application/json"
    if isinstance(data, str):
        return data.encode("utf-8"), "text/plain; charset=utf-8"
    return data, ""


def _request_url(
    base_url: str,
    *,
    path: str,
    params: QueryMapping | Iterable[tuple[str, object]] | None,
) -> str:
    base = urllib.parse.urlsplit(base_url)
    relative = urllib.parse.urlsplit(path)
    if relative.scheme or relative.netloc:
        msg = "request path must be relative"
        raise ValueError(msg)

    normalized_path = base.path.rstrip("/")
    relative_path = relative.path.lstrip("/")
    if relative_path:
        normalized_path = f"{normalized_path}/{relative_path}"

    encoded_params = urllib.parse.urlencode(_query_items(params), doseq=False)
    query = "&".join(part for part in (base.query, relative.query, encoded_params) if part)
    return urllib.parse.urlunsplit(
        (
            base.scheme,
            base.netloc,
            normalized_path,
            query,
            relative.fragment or base.fragment,
        )
    )


def _query_items(
    params: QueryMapping | Iterable[tuple[str, object]] | None,
) -> list[tuple[str, str]]:
    if params is None:
        return []
    raw_items = params.items() if isinstance(params, QueryMapping) else params
    items: list[tuple[str, str]] = []
    for key, value in raw_items:
        if value is None:
            continue
        sequence = _query_sequence(value)
        if sequence is not None:
            items.extend((str(key), item) for item in sequence)
            continue
        items.append((str(key), str(value)))
    return items


def _query_sequence(value: object) -> list[str] | None:
    if not isinstance(value, QuerySequence) or not _is_query_sequence(value):
        return None
    return _stringify_query_sequence(value)


def _is_query_sequence(value: QuerySequence) -> bool:
    return isinstance(value, list | tuple | set)


def _stringify_query_sequence(values: QuerySequence) -> list[str]:
    return [str(item) for item in values]


def _urlopen_response(
    request: urllib.request.Request,
    *,
    timeout_seconds: float,
    ssl_context: ssl.SSLContext,
) -> UrllibResponse:
    response: object = urllib.request.urlopen(
        request,
        timeout=timeout_seconds,
        context=ssl_context,
    )
    if not isinstance(response, UrllibResponse):
        raise HttpTransportError(request.get_method(), request.full_url, "invalid HTTP response")
    return response


def _has_header(headers: Mapping[str, str], name: str) -> bool:
    return any(key.lower() == name.lower() for key in headers)


def _response_headers(headers: Message | None) -> ResponseHeaders:
    values: dict[str, list[str]] = {}
    if headers is None:
        return ResponseHeaders()
    for name in headers:
        normalized = name.lower()
        if normalized in values:
            continue
        values[normalized] = list(headers.get_all(name) or [])
    return ResponseHeaders(values)


__all__ = ["RawHttpResponse", "ReadableBody", "ResponseHeaders", "request_raw"]
