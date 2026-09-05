from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass, field
from email.message import Message
from types import TracebackType
from typing import Protocol

import certifi
from pydantic import JsonValue, TypeAdapter

from shared.http.errors import (
    HttpResponseDecodeError,
    HttpTransportError,
    http_api_error_from_http_error,
)


class _HttpResponse(Protocol):
    status: int
    headers: Message

    def read(self) -> bytes: ...

    def __iter__(self) -> Iterator[bytes]: ...

    def __enter__(self) -> _HttpResponse: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
HTTP_USER_AGENT = "lazycloud/0.1.0"


def build_http_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


@dataclass
class HttpChannel:
    endpoint: str = "http://127.0.0.1:9000"
    token: str | None = None
    timeout_seconds: float = 10.0
    ssl_context: ssl.SSLContext = field(default_factory=build_http_ssl_context, repr=False)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue:
        data = _encode_payload(payload)
        headers = _request_headers(
            self.token,
            {"Content-Type": "application/json"},
        )
        request = urllib.request.Request(
            f"{self.endpoint.rstrip('/')}/{path.lstrip('/')}",
            data=data,
            headers=headers,
            method=method.upper(),
        )
        try:
            opened: _HttpResponse = urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
                context=self.ssl_context,
            )
            with opened as response:
                return _decode_response(response)
        except urllib.error.HTTPError as exc:
            raise http_api_error_from_http_error(exc) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise _transport_error(method, request.full_url, exc) from exc

    def get(self, path: str) -> JsonValue:
        return self.request("GET", path)

    def stream_get(self, path: str) -> Generator[str]:
        headers = _request_headers(self.token)
        request = urllib.request.Request(
            f"{self.endpoint.rstrip('/')}/{path.lstrip('/')}",
            headers=headers,
            method="GET",
        )
        try:
            opened: _HttpResponse = urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
                context=self.ssl_context,
            )
            with opened as response:
                for raw_line in response:
                    yield raw_line.decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise http_api_error_from_http_error(exc) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise _transport_error("GET", request.full_url, exc) from exc

    def post(self, path: str, payload: Mapping[str, JsonValue] | None = None) -> JsonValue:
        return self.request("POST", path, payload=payload)

    def stream_post(
        self,
        path: str,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> Generator[JsonValue]:
        data = _encode_payload(payload)
        headers = _request_headers(
            self.token,
            {
                "Accept": "application/x-ndjson",
                "Content-Type": "application/json",
            },
        )
        request = urllib.request.Request(
            f"{self.endpoint.rstrip('/')}/{path.lstrip('/')}",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            opened: _HttpResponse = urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
                context=self.ssl_context,
            )
            with opened as response:
                for raw_line in response:
                    line = raw_line.strip()
                    if line:
                        yield _decode_json(line)
        except urllib.error.HTTPError as exc:
            raise http_api_error_from_http_error(exc) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise _transport_error("POST", request.full_url, exc) from exc

    def patch(self, path: str, payload: Mapping[str, JsonValue] | None = None) -> JsonValue:
        return self.request("PATCH", path, payload=payload)

    def delete(self, path: str) -> JsonValue:
        return self.request("DELETE", path)


def _decode_response(response: _HttpResponse) -> JsonValue:
    if response.status == 204:
        return None
    raw = response.read().decode("utf-8")
    if not raw:
        return None
    content_type = response.headers.get("Content-Type", "")
    if "application/json" in content_type:
        return _decode_json(raw)
    return raw


def _encode_payload(payload: Mapping[str, JsonValue] | None) -> bytes | None:
    if payload is None:
        return None
    value = _JSON_VALUE_ADAPTER.validate_python(dict(payload))
    return json.dumps(value).encode("utf-8")


def _request_headers(
    token: str | None,
    headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    merged = {"User-Agent": HTTP_USER_AGENT}
    if token:
        merged["Authorization"] = f"Bearer {token}"
    if headers:
        merged.update(headers)
    return merged


def _decode_json(raw: str | bytes) -> JsonValue:
    try:
        return _JSON_VALUE_ADAPTER.validate_json(raw)
    except ValueError as exc:
        raise HttpResponseDecodeError("HTTP response contained invalid JSON") from exc


def _transport_error(
    method: str,
    url: str,
    exc: OSError | http.client.HTTPException,
) -> HttpTransportError:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return HttpTransportError(method, url, str(reason))
