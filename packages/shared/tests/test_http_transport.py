from __future__ import annotations

import ssl
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

import pytest
from shared.client_version import client_version
from shared.http.errors import HttpApiError, HttpResponseDecodeError, HttpTransportError
from shared.http_transport import HttpChannel, build_http_ssl_context
from tests.http_server import running_http_server


@dataclass(frozen=True)
class ReceivedRequest:
    path: str
    port: int
    token: str | None
    user_agent: str | None
    body: bytes


class TransportServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                server.respond(self)

            def do_POST(self) -> None:
                server.respond(self)

            def log_message(self, format: str, *args: object) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: list[ReceivedRequest] = []
        self.request_lock = Lock()
        self.redirect_url = ""

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def respond(self, handler: BaseHTTPRequestHandler) -> None:
        with self.request_lock:
            self.requests.append(
                ReceivedRequest(
                    handler.path,
                    handler.client_address[1],
                    handler.headers.get("Authorization"),
                    handler.headers.get("User-Agent"),
                    handler.rfile.read(int(handler.headers.get("Content-Length", "0"))),
                )
            )
        if handler.path == "/lost-post":
            handler.close_connection = True
            return
        body = b'{"ok":true}'
        status = 200
        if handler.path == "/error":
            status, body = 409, b'{"detail":"claim lost","code":"conflict"}'
        elif handler.path == "/bad-json":
            body = b"{"
        elif handler.path == "/lines":
            body = "first\r\n\nlast é".encode()
        elif handler.path == "/ndjson":
            body = b'{"value":1}\n\n{"value":2}'
        elif handler.path == "/partial":
            body = b"first\n"
        elif handler.path == "/timeout":
            body = b""
        elif handler.path == "/redirect":
            status, body = 302, b""
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        declared_length = len(body) + (100 if handler.path in {"/partial", "/timeout"} else 0)
        handler.send_header("Content-Length", str(declared_length))
        if handler.path == "/redirect":
            handler.send_header("Location", self.redirect_url)
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()


def test_channel_reuses_connections_and_preserves_http_contracts() -> None:
    server = TransportServer()
    with (
        running_http_server(server),
        HttpChannel(endpoint=server.endpoint, token="test-token") as channel,
    ):
        assert channel.post("/json", {"value": 1}) == {"ok": True}
        assert list(channel.stream_get("/lines")) == ["first\r\n", "\n", "last é"]
        assert list(channel.stream_post("/ndjson", {})) == [{"value": 1}, {"value": 2}]
        with pytest.raises(HttpApiError) as failure:
            channel.get("/error")
        assert (failure.value.status_code, failure.value.code) == (409, "conflict")
        with pytest.raises(HttpResponseDecodeError):
            channel.get("/bad-json")
        assert len({request.port for request in server.requests}) == 1
        assert all(request.token == "Bearer test-token" for request in server.requests)
        assert all(
            request.user_agent == f"lazycloud/{client_version()}" for request in server.requests
        )
        assert server.requests[0].body == b'{"value": 1}'
        with ThreadPoolExecutor(max_workers=4) as executor:
            assert list(executor.map(channel.get, ["/json"] * 8)) == [{"ok": True}] * 8
    with pytest.raises(RuntimeError, match="closed"):
        channel.get("/json")


def test_channel_discards_abandoned_streams_and_does_not_replay_failed_posts() -> None:
    server = TransportServer()
    with running_http_server(server), HttpChannel(endpoint=server.endpoint) as channel:
        lines = channel.stream_get("/partial")
        assert next(lines) == "first\n"
        lines.close()
        assert channel.get("/json") == {"ok": True}
        assert server.requests[0].port != server.requests[1].port
        with pytest.raises(HttpTransportError) as failure:
            channel.post("/lost-post", {"charged": True})
        assert failure.value.method == "POST"
        assert sum(request.path == "/lost-post" for request in server.requests) == 1
        with pytest.raises(HttpTransportError):
            channel.post("/timeout", {}, timeout_seconds=0.02)
        assert channel.get("/json") == {"ok": True}


def test_channel_does_not_forward_bearer_token_to_redirect_origin() -> None:
    source, destination = TransportServer(), TransportServer()
    source.redirect_url = destination.endpoint + "/json"
    with (
        running_http_server(source),
        running_http_server(destination),
        HttpChannel(endpoint=source.endpoint, token="test-token") as channel,
    ):
        assert channel.get("/redirect") == {"ok": True}
    assert source.requests[0].token == "Bearer test-token"
    assert destination.requests[0].token is None


def test_channel_uses_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = TransportServer()
    monkeypatch.setenv("HTTP_PROXY", proxy.endpoint)
    monkeypatch.setenv("NO_PROXY", "")
    with running_http_server(proxy), HttpChannel(endpoint="http://upstream.invalid") as channel:
        assert channel.get("/json") == {"ok": True}
    assert proxy.requests[0].path == "http://upstream.invalid/json"


def test_channel_requires_trusted_certificate_and_matching_hostname(tmp_path: Path) -> None:
    key, certificate = tmp_path / "key.pem", tmp_path / "certificate.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-noenc",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server = TransportServer()
    server.socket = context.wrap_socket(server.socket, server_side=True)
    endpoint = f"https://localhost:{server.server_port}"
    trusted = build_http_ssl_context()
    trusted.load_verify_locations(certificate)
    with running_http_server(server):
        with HttpChannel(endpoint=endpoint) as channel, pytest.raises(HttpTransportError):
            channel.get("/json")
        with HttpChannel(endpoint=endpoint, ssl_context=trusted) as channel:
            assert channel.get("/json") == {"ok": True}
        with (
            HttpChannel(
                endpoint=f"https://127.0.0.1:{server.server_port}", ssl_context=trusted
            ) as channel,
            pytest.raises(HttpTransportError),
        ):
            channel.get("/json")
