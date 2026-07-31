from __future__ import annotations

import re
import socket
from collections.abc import Iterator

import pytest
from agent_app.route_proxy import AgentRouteProxyConfig, AgentRouteProxyService
from gateway.http import UpdateAgentRouteStatusRequest, UpdateAgentRouteStatusResponse
from networking.agent_peer_client import AgentPeerClient, AgentPeerResolutionError


class _Routes:
    """The route status client the proxy reports to; unused by resolution."""

    def update_agent_route_status(
        self,
        request: UpdateAgentRouteStatusRequest,
    ) -> UpdateAgentRouteStatusResponse:
        del request
        raise AssertionError("resolution must not report route status")


class _Resolver:
    """Resolves a peer only for a caller presenting a known worker token."""

    def __init__(self, token: str, host: str, address: str) -> None:
        self.token = token
        self.host = host
        self.address = address

    def resolve_for_worker(self, host: str, worker_token: str) -> str:
        if worker_token != self.token:
            raise PermissionError("unknown worker token")
        return self.address if host == self.host else ""


@pytest.fixture
def proxy() -> Iterator[tuple[AgentRouteProxyService, str]]:
    service = AgentRouteProxyService(
        config=AgentRouteProxyConfig(bind_host="127.0.0.1", bind_port=0),
        client=_Routes(),
        agent_token="agent-token",
        peer_resolver=_Resolver("worker-token", "peer.example.ts.net", "100.64.0.9"),
    )
    service.start()
    try:
        yield service, service.proxy_target
    finally:
        service.close()


def test_a_worker_resolves_a_peer_through_the_agent(
    proxy: tuple[AgentRouteProxyService, str],
) -> None:
    """The worker has no tailnet client, so the agent answers on its behalf."""
    _service, address = proxy
    client = AgentPeerClient(agent_address=address, worker_token="worker-token")

    assert client.resolve_peer_host("peer.example.ts.net") == "100.64.0.9"


def test_an_unknown_worker_token_is_refused(
    proxy: tuple[AgentRouteProxyService, str],
) -> None:
    """The channel is loopback, but loopback is shared with user workloads."""
    _service, address = proxy
    client = AgentPeerClient(agent_address=address, worker_token="not-the-token")

    with pytest.raises(AgentPeerResolutionError):
        client.resolve_peer_host("peer.example.ts.net")


def test_an_unknown_peer_is_named_rather_than_returned_empty(
    proxy: tuple[AgentRouteProxyService, str],
) -> None:
    """Returning nothing would leave the caller to dial a name that may resolve elsewhere."""
    _service, address = proxy
    client = AgentPeerClient(agent_address=address, worker_token="worker-token")

    with pytest.raises(AgentPeerResolutionError, match=re.escape("other.example.ts.net")):
        client.resolve_peer_host("other.example.ts.net")


def test_route_proxying_still_works_alongside_resolution(
    proxy: tuple[AgentRouteProxyService, str],
) -> None:
    """Resolution shares the listener, so it must not shadow the route preface."""
    _service, address = proxy
    host, _, port = address.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=5) as connection:
        connection.sendall(b"BACKEND-ROUTE/2 route-1 credential\n")
        connection.settimeout(5)
        # An unauthorized route closes without a resolution error reply.
        assert not connection.recv(64).startswith(b"ERROR")


def test_resolution_does_not_proxy_traffic(
    proxy: tuple[AgentRouteProxyService, str],
) -> None:
    """The agent answers with an address; the worker dials the peer itself.

    Carrying the bytes would put the agent in the path of image-archive
    transfers, which is the bottleneck shape this design avoids.
    """
    service, address = proxy
    host, _, port = address.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=5) as connection:
        connection.sendall(b"TAILNET-RESOLVE/1 peer.example.ts.net worker-token\n")
        connection.settimeout(5)
        reply = connection.recv(128)
    assert reply == b"100.64.0.9\n"
    del service
