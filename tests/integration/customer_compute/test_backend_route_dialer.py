from __future__ import annotations

import socket
from dataclasses import dataclass, field

import pytest
from execution.shells.proxy import ShellBackendTarget
from gateway.shell_proxy import connect_shell_backend
from networking.dialer import (
    BackendRouteDialer,
    BackendRouteDialerConfig,
    SocketBackendConnector,
)
from networking.routing import (
    BackendRouteAuthenticator,
    backend_route_preface,
    build_backend_route_dial_plan,
)
from pydantic import SecretStr
from shared.routing import AgentBackendRoute, BackendRouteState, BackendRouteTransport

ROUTE_AUTH_KEY = SecretStr("0123456789abcdef0123456789abcdef")
ROUTE_AUTHENTICATOR = BackendRouteAuthenticator(ROUTE_AUTH_KEY)


def _dialer_config(**updates: float) -> BackendRouteDialerConfig:
    return BackendRouteDialerConfig(
        timeout_seconds=updates.get("timeout_seconds", 1),
        ready_poll_seconds=updates.get("ready_poll_seconds", 0.001),
        auth_key=ROUTE_AUTH_KEY,
    )


@dataclass(slots=True)
class _FakeConnection:
    writes: list[bytes] = field(default_factory=list)
    closed: bool = False

    def sendall(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True


class _FakeConnector:
    def __init__(self, *, failures_before_success: int = 0) -> None:
        self.calls: list[tuple[str, float]] = []
        self.connections: list[_FakeConnection] = []
        self.failures_before_success = failures_before_success

    def connect(self, address: str, timeout_seconds: float) -> _FakeConnection:
        self.calls.append((address, timeout_seconds))
        if self.failures_before_success > 0:
            self.failures_before_success -= 1
            raise OSError("temporary dial failure")
        connection = _FakeConnection()
        self.connections.append(connection)
        return connection


class _FakeTailnetPeerWaiter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def wait_for_peer(self, host: str, timeout_seconds: float) -> None:
        self.calls.append((host, timeout_seconds))


class _FakeTailnetPeerResolver:
    def __init__(self, resolved_host: str) -> None:
        self.resolved_host = resolved_host
        self.calls: list[str] = []

    def resolve_peer_host(self, host: str) -> str:
        self.calls.append(host)
        return self.resolved_host


class _FakeRouteResolver:
    def __init__(self, routes: list[AgentBackendRoute | None]) -> None:
        self.routes = routes
        self.route_ids: list[str] = []

    def get_backend_route(self, route_id: str) -> AgentBackendRoute | None:
        self.route_ids.append(route_id)
        if len(self.routes) > 1:
            return self.routes.pop(0)
        return self.routes[0]


def test_backend_route_dialer_waits_for_ready_route_and_writes_preface() -> None:
    connector = _FakeConnector()
    resolver = _FakeRouteResolver(
        [
            _route("route-one", state=BackendRouteState.Opening),
            _route(
                "route-one",
                state=BackendRouteState.Ready,
                proxy_target="agent.tailnet:29443",
                transport=BackendRouteTransport.TsnetRestricted,
            ),
        ]
    )
    dialer = BackendRouteDialer(
        resolver,
        config=_dialer_config(),
        connector=connector,
    )

    connection = dialer.dial_plan(build_backend_route_dial_plan("route-one"))

    assert connection is connector.connections[0]
    assert resolver.route_ids == ["route-one", "route-one"]
    assert connector.calls[0][0] == "agent.tailnet:29443"
    assert connector.connections[0].writes == [
        backend_route_preface("route-one", ROUTE_AUTHENTICATOR.credential("route-one"))
    ]


@pytest.mark.parametrize(
    ("target", "transport", "failures", "resolved", "expected_addresses"),
    [
        (
            "agent.tailnet:29443",
            BackendRouteTransport.TsnetRestricted,
            1,
            "100.64.0.2",
            ["100.64.0.2:29443", "100.64.0.2:29443"],
        ),
        (
            "100.64.0.10:29443",
            BackendRouteTransport.TsnetRestricted,
            0,
            "100.64.0.99",
            ["100.64.0.10:29443"],
        ),
        ("10.0.0.5:8000", BackendRouteTransport.Direct, 0, "unused", ["10.0.0.5:8000"]),
        (
            "container-worker:57267",
            BackendRouteTransport.Direct,
            1,
            "unused",
            ["container-worker:57267", "container-worker:57267"],
        ),
    ],
)
def test_backend_route_dialer_transport_retry_matrix(
    target: str,
    transport: BackendRouteTransport,
    failures: int,
    resolved: str,
    expected_addresses: list[str],
) -> None:
    connector = _FakeConnector(failures_before_success=failures)
    waiter = _FakeTailnetPeerWaiter()
    resolver = _FakeTailnetPeerResolver(resolved)
    route_id = "route-transport"
    dialer = BackendRouteDialer(
        _FakeRouteResolver(
            [
                _route(
                    route_id,
                    state=BackendRouteState.Ready,
                    proxy_target=target,
                    transport=transport,
                )
            ]
        ),
        config=_dialer_config(),
        connector=connector,
        tailnet_peer_waiter=waiter,
        tailnet_peer_resolver=resolver,
    )

    dialer.dial_plan(build_backend_route_dial_plan(route_id))

    assert [address for address, _timeout in connector.calls] == expected_addresses
    if transport is BackendRouteTransport.TsnetRestricted:
        # A process holding a peer waiter cannot resolve tailnet names itself,
        # so every attempt goes through the netmap rather than public DNS.
        assert [call[0] for call in waiter.calls] == [target.rsplit(":", 1)[0]] * (failures + 1)
        assert connector.connections[0].writes == [
            backend_route_preface(route_id, ROUTE_AUTHENTICATOR.credential(route_id))
        ]
    else:
        assert waiter.calls == []
        assert resolver.calls == []
        assert connector.connections[0].writes == []


def test_backend_route_dialer_rejects_missing_authenticator_before_proxying() -> None:
    connector = _FakeConnector()
    dialer = BackendRouteDialer(
        _FakeRouteResolver(
            [
                _route(
                    "route-tailnet",
                    state=BackendRouteState.Ready,
                    proxy_target="agent.tailnet:29443",
                    transport=BackendRouteTransport.TsnetRestricted,
                )
            ]
        ),
        config=BackendRouteDialerConfig(timeout_seconds=1, ready_poll_seconds=0.001),
        connector=connector,
    )

    with pytest.raises(RuntimeError, match="authenticator is required"):
        dialer.dial_plan(build_backend_route_dial_plan("route-tailnet"))

    assert connector.calls == []
    assert connector.connections == []


def test_backend_route_dialer_rejects_unusable_routes() -> None:
    degraded = BackendRouteDialer(
        _FakeRouteResolver([_route("route-bad", state=BackendRouteState.Degraded)]),
        config=BackendRouteDialerConfig(timeout_seconds=1, ready_poll_seconds=0.001),
        connector=_FakeConnector(),
    )
    with pytest.raises(RuntimeError, match="backend route route-bad is degraded"):
        degraded.dial_plan(build_backend_route_dial_plan("route-bad"))

    missing = BackendRouteDialer(
        _FakeRouteResolver([None]),
        config=BackendRouteDialerConfig(timeout_seconds=1, ready_poll_seconds=0.001),
        connector=_FakeConnector(),
    )
    with pytest.raises(RuntimeError, match="backend route route-missing not found"):
        missing.dial_plan(build_backend_route_dial_plan("route-missing"))


def test_shell_backend_uses_authoritative_route_with_raw_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, peer = socket.socketpair()
    calls: list[tuple[str, float]] = []

    def connect(
        _connector: SocketBackendConnector,
        address: str,
        timeout_seconds: float,
    ) -> socket.socket:
        calls.append((address, timeout_seconds))
        return connection

    monkeypatch.setattr(SocketBackendConnector, "connect", connect)
    route_id = "machine:worker:container:container:2222"
    resolver = _FakeRouteResolver(
        [
            _route(
                route_id,
                state=BackendRouteState.Ready,
                proxy_target="container-worker:50819",
                transport=BackendRouteTransport.Direct,
            )
        ]
    )
    target = ShellBackendTarget(
        container_id="container",
        stub_id="stub",
        address="192.168.0.85:2222",
        route=AgentBackendRoute(
            route_id=route_id,
            container_id="container",
            port=2222,
            transport=BackendRouteTransport.Direct,
            state=BackendRouteState.Ready,
            local_target="container-worker:50819",
            proxy_target="container-worker:50819",
        ),
        worker_port=2222,
        buffer_size_bytes=32 * 1024,
        dial_timeout_seconds=1,
    )

    try:
        connected = connect_shell_backend(target, route_resolver=resolver)

        assert connected is connection
        assert resolver.route_ids == [route_id]
        assert calls[0][0] == "container-worker:50819"
        assert 0 < calls[0][1] <= 1
    finally:
        connection.close()
        peer.close()


def test_shell_backend_authenticates_tailnet_route_before_shell_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, peer = socket.socketpair()

    def connect(
        _connector: SocketBackendConnector,
        address: str,
        timeout_seconds: float,
    ) -> socket.socket:
        assert address == "agent.tailnet:29443"
        assert 0 < timeout_seconds <= 1
        return connection

    monkeypatch.setattr(SocketBackendConnector, "connect", connect)
    route_id = "machine:worker:container:container:2222"
    route = _route(
        route_id,
        state=BackendRouteState.Ready,
        proxy_target="agent.tailnet:29443",
        transport=BackendRouteTransport.TsnetRestricted,
    )
    target = ShellBackendTarget(
        container_id="container",
        stub_id="stub",
        address=f"route://{route_id}",
        route=AgentBackendRoute.model_validate(route.model_dump(mode="json")),
        worker_port=2222,
        buffer_size_bytes=32 * 1024,
        dial_timeout_seconds=1,
    )

    try:
        connected = connect_shell_backend(
            target,
            route_resolver=_FakeRouteResolver([route]),
            route_dialer_config=_dialer_config(),
        )

        assert connected is connection
        assert peer.recv(4096) == backend_route_preface(
            route_id,
            ROUTE_AUTHENTICATOR.credential(route_id),
        )
    finally:
        connection.close()
        peer.close()


def _route(
    route_id: str,
    *,
    state: BackendRouteState,
    proxy_target: str = "",
    transport: BackendRouteTransport = BackendRouteTransport.TsnetRestricted,
) -> AgentBackendRoute:
    return AgentBackendRoute(
        route_id=route_id,
        workspace_id="workspace-one",
        pool_name="gpu",
        machine_id="machine-one",
        state=state,
        proxy_target=proxy_target,
        transport=transport,
    )
