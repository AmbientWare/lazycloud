from __future__ import annotations

import json
import os
import ssl
import weakref
from collections.abc import Generator, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import TracebackType

import certifi
import httpx
from pydantic import JsonValue, TypeAdapter

from shared.client_version import (
    RECOMMENDED_CLIENT_VERSION_HEADER,
    client_version,
    report_client_version,
)
from shared.http.errors import (
    HttpResponseDecodeError,
    HttpTransportError,
    http_api_error_from_body,
)

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    payload: JsonValue
    headers: Mapping[str, str]


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
    _client: httpx.Client = field(init=False, repr=False)
    _owner_pid: int = field(default_factory=os.getpid, init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            verify=self.ssl_context,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=None),
        )
        weakref.finalize(self, self._client.close)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpChannel:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _request_url(self, path: str) -> str:
        if os.getpid() != self._owner_pid:
            raise RuntimeError("HTTP channels must be created in the process that uses them")
        return f"{self.endpoint.rstrip('/')}/{path.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, JsonValue] | None = None,
        timeout_seconds: float | None = None,
    ) -> JsonValue:
        return self.request_response(
            method, path, payload=payload, timeout_seconds=timeout_seconds
        ).payload

    def request_response(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, JsonValue] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> HttpResponse:
        data = _encode_payload(payload)
        request_headers = _request_headers(
            self.token,
            {"Content-Type": "application/json", **(headers or {})},
        )
        url = self._request_url(path)
        try:
            with self._client.stream(
                method.upper(),
                url,
                content=data,
                headers=request_headers,
                timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
            ) as response:
                _check_response(response)
                return HttpResponse(_decode_response(response), response.headers)
        except httpx.RequestError as exc:
            raise HttpTransportError(method, url, str(exc)) from exc

    def get(self, path: str) -> JsonValue:
        return self.request("GET", path)

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        data: bytes | Iterable[bytes],
        headers: Mapping[str, str],
        timeout_seconds: float | None = None,
    ) -> bytes:
        url = self._request_url(path)
        try:
            with self._client.stream(
                method.upper(),
                url,
                content=data,
                headers=_request_headers(self.token, headers),
                timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
            ) as response:
                _check_response(response)
                return response.read()
        except httpx.RequestError as exc:
            raise HttpTransportError(method, url, str(exc)) from exc

    def stream_get(self, path: str, *, timeout_seconds: float | None = None) -> Generator[str]:
        headers = _request_headers(self.token)
        url = self._request_url(path)
        try:
            with self._client.stream(
                "GET",
                url,
                headers=headers,
                timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
            ) as response:
                _check_response(response)
                for raw_line in _response_lines(response):
                    yield raw_line.decode("utf-8")
        except httpx.RequestError as exc:
            raise HttpTransportError("GET", url, str(exc)) from exc

    def post(
        self,
        path: str,
        payload: Mapping[str, JsonValue] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> JsonValue:
        return self.request("POST", path, payload=payload, timeout_seconds=timeout_seconds)

    def stream_post(
        self,
        path: str,
        payload: Mapping[str, JsonValue] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Generator[JsonValue]:
        data = _encode_payload(payload)
        headers = _request_headers(
            self.token,
            {
                "Accept": "application/x-ndjson",
                "Content-Type": "application/json",
            },
        )
        url = self._request_url(path)
        try:
            with self._client.stream(
                "POST",
                url,
                content=data,
                headers=headers,
                timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
            ) as response:
                _check_response(response)
                for raw_line in _response_lines(response):
                    line = raw_line.strip()
                    if line:
                        yield _decode_json(line)
        except httpx.RequestError as exc:
            raise HttpTransportError("POST", url, str(exc)) from exc

    def patch(self, path: str, payload: Mapping[str, JsonValue] | None = None) -> JsonValue:
        return self.request("PATCH", path, payload=payload)

    def put(self, path: str, payload: Mapping[str, JsonValue] | None = None) -> JsonValue:
        return self.request("PUT", path, payload=payload)

    def delete(self, path: str) -> JsonValue:
        return self.request("DELETE", path)


def _check_response(response: httpx.Response) -> None:
    report_client_version(response.headers.get(RECOMMENDED_CLIENT_VERSION_HEADER))
    if not response.is_success:
        raise http_api_error_from_body(
            response.status_code,
            response.read().decode("utf-8", errors="replace"),
            fallback=f"HTTP {response.status_code}: {response.reason_phrase}",
        )


def _response_lines(response: httpx.Response) -> Iterator[bytes]:
    pending = b""
    for chunk in response.iter_bytes():
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", maxsplit=1)
            yield line + b"\n"
    if pending:
        yield pending


def _decode_response(response: httpx.Response) -> JsonValue:
    if response.status_code == 204:
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
    version = client_version()
    merged = {"User-Agent": f"lazycloud/{version}"}
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
