from __future__ import annotations

from fastapi import Request, WebSocket


def forwarded_path(subpath: str) -> str:
    if not subpath:
        return "/"
    return subpath if subpath.startswith("/") else f"/{subpath}"


def request_query_params(request: Request) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        values.setdefault(key, []).append(value)
    return values


def websocket_query_params(websocket: WebSocket) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for key, value in websocket.query_params.multi_items():
        values.setdefault(key, []).append(value)
    return values


def request_headers(request: Request) -> dict[str, list[str]]:
    return _raw_headers(request.headers.raw)


def websocket_headers(websocket: WebSocket) -> dict[str, list[str]]:
    return _raw_headers(websocket.headers.raw)


def _raw_headers(raw_headers: list[tuple[bytes, bytes]]) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for raw_key, raw_value in raw_headers:
        key = raw_key.decode("latin-1")
        value = raw_value.decode("latin-1")
        headers.setdefault(key, []).append(value)
    return headers
