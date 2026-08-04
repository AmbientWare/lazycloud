from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from networking.internal_http import (
    InternalHttpClient,
    InternalHttpError,
    TailnetHostPolicy,
    TailnetPeerAddresses,
)


class _EchoHost(BaseHTTPRequestHandler):
    """Answers with the Host header it received."""

    def do_GET(self) -> None:
        body = self.headers.get("Host", "").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.fixture
def echo_server() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _EchoHost)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()


class _Peers:
    """A tailnet runtime that knows one peer."""

    def __init__(self, host: str, address: str) -> None:
        self.host = host
        self.address = address
        self.waited: list[str] = []

    def wait_for_peer(self, host: str, timeout_seconds: float) -> None:
        del timeout_seconds
        self.waited.append(host)

    def resolve_peer_host(self, host: str) -> str:
        return self.address if host == self.host else ""

    def start(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_a_tailnet_host_is_dialed_at_its_peer_address_but_keeps_its_host_header(
    echo_server: int,
) -> None:
    """The signature on an object-store URL covers Host, so only the address may move."""
    peer = "control-plane.tailnet-example.ts.net"
    runtime = _Peers(peer, "127.0.0.1")
    addresses = TailnetPeerAddresses(
        runtime=runtime,
        policy=TailnetHostPolicy(dns_suffix="tailnet-example.ts.net"),
    )
    client = InternalHttpClient(addresses=addresses, timeout_seconds=5.0)

    try:
        response = client.request("GET", f"http://{peer}:{echo_server}/")
    finally:
        client.close()

    assert response.status_code == 200
    # Reached a server that only listens on loopback, while the request still
    # named the peer.
    assert response.text == f"{peer}:{echo_server}"
    # The name does not resolve, so the address came from the recovery path.
    assert runtime.waited == [peer]


def test_a_peer_address_is_resolved_once_and_reused(echo_server: int) -> None:
    """A netmap round trip per request is what takes the control session down.

    Three failed lookups inside a minute reach the refresh that runs `tailscale
    down`, so a destination already resolved must not be looked up again.
    """
    peer = "control-plane.tailnet-example.ts.net"
    runtime = _Peers(peer, "127.0.0.1")
    addresses = TailnetPeerAddresses(
        runtime=runtime,
        policy=TailnetHostPolicy(dns_suffix="tailnet-example.ts.net"),
    )
    client = InternalHttpClient(addresses=addresses, timeout_seconds=5.0)

    try:
        for _ in range(3):
            assert client.request("GET", f"http://{peer}:{echo_server}/").status_code == 200
    finally:
        client.close()

    assert runtime.waited == [peer]


def test_a_host_outside_the_tailnet_is_dialed_as_written(echo_server: int) -> None:
    """Local service names must not be routed through peer resolution."""
    runtime = _Peers("control-plane.tailnet-example.ts.net", "127.0.0.1")
    addresses = TailnetPeerAddresses(
        runtime=runtime,
        policy=TailnetHostPolicy(dns_suffix="tailnet-example.ts.net"),
    )
    client = InternalHttpClient(addresses=addresses, timeout_seconds=5.0)

    try:
        response = client.request("GET", f"http://127.0.0.1:{echo_server}/")
    finally:
        client.close()

    assert response.status_code == 200
    assert runtime.waited == []


def test_an_unresolvable_tailnet_peer_fails_and_names_it(echo_server: int) -> None:
    """A silent fallback to the name would surface far from the real cause."""
    del echo_server
    addresses = TailnetPeerAddresses(
        runtime=_Peers("other.tailnet-example.ts.net", "127.0.0.1"),
        policy=TailnetHostPolicy(dns_suffix="tailnet-example.ts.net"),
    )
    client = InternalHttpClient(addresses=addresses, timeout_seconds=5.0)

    try:
        with pytest.raises(InternalHttpError, match=re.escape("missing.tailnet-example.ts.net")):
            client.request("GET", "http://missing.tailnet-example.ts.net/health")
    finally:
        client.close()
