"""A local HTTP server that answers the public API routes a test registers.

It stands in for the platform only in focused client tests; the SDK's
end-to-end acceptance runs against the real server.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from tests.http_server import running_http_server

TOKEN = "lc_test-token-0123456789"
WORKSPACE = "team"


@dataclass(frozen=True)
class ApiRequest:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes

    def json(self) -> object:
        return json.loads(self.body)


class StreamAborted(Exception):
    """Raised by a streaming route to drop the connection mid-response."""


Reply = tuple[int, dict[str, str], bytes | Iterable[bytes]]
Route = Callable[[ApiRequest], Reply]


def json_reply(payload: object, status: int = 200) -> Reply:
    return status, {"Content-Type": "application/json"}, json.dumps(payload).encode()


def error_reply(code: str, message: str, status: int) -> Reply:
    return json_reply({"code": code, "message": message}, status)


@dataclass
class FakeApi:
    url: str = ""
    routes: list[tuple[str, re.Pattern[str], Route]] = field(default_factory=list)
    requests: list[ApiRequest] = field(default_factory=list)

    def route(self, method: str, pattern: str) -> Callable[[Route], Route]:
        def register(handler: Route) -> Route:
            self.routes.insert(0, (method, re.compile(pattern), handler))
            return handler

        return register

    def calls(self, method: str, pattern: str) -> list[ApiRequest]:
        compiled = re.compile(pattern)
        return [r for r in self.requests if r.method == method and compiled.fullmatch(r.path)]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        request = ApiRequest(
            method=self.command,
            path=parts.path,
            query=parse_qs(parts.query),
            headers={name.lower(): value for name, value in self.headers.items()},
            body=self.rfile.read(length) if length else b"",
        )
        api = self.server.api
        api.requests.append(request)
        for method, pattern, handler in api.routes:
            if method == request.method and pattern.fullmatch(request.path):
                status, headers, body = handler(request)
                break
        else:
            status, headers, body = error_reply("not_found", "no such route", 404)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        if isinstance(body, bytes):
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for chunk in body:
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
        except StreamAborted:
            self.close_connection = True
            return
        self.wfile.write(b"0\r\n\r\n")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    api: FakeApi


@contextmanager
def running_fake_api() -> Iterator[FakeApi]:
    server = _Server(("127.0.0.1", 0), _Handler)
    server.api = FakeApi(url=f"http://127.0.0.1:{server.server_address[1]}")
    with running_http_server(server):
        yield server.api
