from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlencode

HOP_BY_HOP_REQUEST_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def forwarded_request_path(path: str, query_params: dict[str, list[str]]) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    query = urlencode([(key, value) for key, values in query_params.items() for value in values])
    return f"{normalized_path}?{query}" if query else normalized_path


def forwarded_request_headers(headers: dict[str, list[str]]) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for key, values in headers.items():
        if key.lower() in HOP_BY_HOP_REQUEST_HEADERS or not values:
            continue
        forwarded[key] = ", ".join(values)
    return forwarded


def grouped_response_headers(headers: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for key, value in headers:
        result.setdefault(key, []).append(value)
    return result
