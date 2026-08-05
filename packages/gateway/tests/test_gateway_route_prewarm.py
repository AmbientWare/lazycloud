from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event as ThreadEvent

import pytest
from compute.agent_control import (
    ComputeAgentTokenState,
    TailnetPeerView,
)
from gateway.route_prewarm import RoutePrewarmService, ThreadRoutePrewarmRunner
from networking.dialer import BackendRouteDialer, BackendRouteDialerConfig
from networking.routing import BackendRouteAuthenticator, backend_route_preface
from pydantic import JsonValue, SecretStr
from shared.compute_policy import MachinePool
from shared.events import Event
from shared.routing import (
    AgentBackendRoute,
    BackendRouteState,
    BackendRouteTransport,
    RoutePrewarmDecision,
)

ROUTE_AUTH_KEY = SecretStr("0123456789abcdef0123456789abcdef")
ROUTE_AUTHENTICATOR = BackendRouteAuthenticator(ROUTE_AUTH_KEY)


@dataclass(slots=True)
class _Connection:
    writes: list[bytes] = field(default_factory=list)
    closed: bool = False

    def sendall(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class _Connector:
    error: OSError | None = None
    connections: list[_Connection] = field(default_factory=list)
    calls: list[tuple[str, float]] = field(default_factory=list)

    def connect(self, address: str, timeout_seconds: float) -> _Connection:
        self.calls.append((address, timeout_seconds))
        if self.error is not None:
            raise self.error
        connection = _Connection()
        self.connections.append(connection)
        return connection


@dataclass(slots=True)
class _InlineRunner:
    def submit(self, task: Callable[[], None]) -> None:
        task()


@dataclass(slots=True)
class _Events:
    records: list[tuple[str, dict[str, JsonValue] | None, str | None]] = field(default_factory=list)

    def emit(
        self,
        action: str,
        *,
        resource_type: str,
        resource_id: str,
        message: str,
        data: dict[str, JsonValue] | None = None,
        workspace_id: str | None = None,
    ) -> Event:
        _ = resource_type, resource_id, message
        self.records.append((action, data, workspace_id))
        return Event(
            id="event-one",
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            message=message,
            data=data or {},
        )


@dataclass(slots=True)
class _PeerProvider:
    peer_list: list[TailnetPeerView]

    def peers(self) -> list[TailnetPeerView]:
        return self.peer_list


def test_route_prewarm_dials_route_writes_preface_emits_event_and_throttles() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    connector = _Connector()
    events = _Events()
    prewarmer = RoutePrewarmService(
        BackendRouteDialer(
            config=BackendRouteDialerConfig(
                timeout_seconds=1,
                ready_poll_seconds=0.001,
                auth_key=ROUTE_AUTH_KEY,
            ),
            connector=connector,
        ),
        events,
        runner=_InlineRunner(),
        interval_seconds=30,
    )

    first = prewarmer.prewarm_route(_route(), _agent(), now=now)
    second = prewarmer.prewarm_route(_route(), _agent(), now=now + timedelta(seconds=5))

    assert first.decision is RoutePrewarmDecision.Attempt
    assert second.decision is RoutePrewarmDecision.Throttled
    assert connector.calls[0][0] == "agent.tailnet:29443"
    assert connector.connections[0].writes == [
        backend_route_preface("route-one", ROUTE_AUTHENTICATOR.credential("route-one"))
    ]
    assert connector.connections[0].closed is True
    action, data, workspace_id = events.records[0]
    assert action == "transport.prewarm"
    assert workspace_id == "workspace-one"
    assert data is not None
    assert data["status"] == "ready"
    assert data["route_id"] == "route-one"
    attrs = data["attrs"]
    assert isinstance(attrs, dict)
    assert attrs["proxy_target"] == "agent.tailnet:29443"


def test_route_prewarm_emits_error_with_peer_status() -> None:
    connector = _Connector(error=OSError("dial timeout"))
    events = _Events()
    prewarmer = RoutePrewarmService(
        BackendRouteDialer(
            config=BackendRouteDialerConfig(
                timeout_seconds=0.01,
                ready_poll_seconds=0.001,
                auth_key=ROUTE_AUTH_KEY,
            ),
            connector=connector,
        ),
        events,
        runner=_InlineRunner(),
        peer_provider=_PeerProvider(
            [
                TailnetPeerView(
                    dns_name="agent.tailnet.",
                    online=True,
                    active=True,
                    current_address="203.0.113.10:1234",
                )
            ]
        ),
    )

    attempt = prewarmer.prewarm_route(_route(), _agent())

    assert attempt.decision is RoutePrewarmDecision.Attempt
    _action, data, _workspace_id = events.records[0]
    assert data is not None
    assert data["status"] == "error"
    attrs = data["attrs"]
    assert isinstance(attrs, dict)
    reason = attrs["reason"]
    assert isinstance(reason, str)
    assert "dial timeout" in reason
    assert attrs["peer_online"] == "true"


def test_thread_route_prewarm_runner_quiesces_before_close() -> None:
    completed = ThreadEvent()
    runner = ThreadRoutePrewarmRunner()

    runner.submit(completed.set)
    runner.close()

    assert completed.is_set()
    with pytest.raises(RuntimeError, match="closed"):
        runner.submit(lambda: None)


def _agent() -> ComputeAgentTokenState:
    return ComputeAgentTokenState(
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        token_hash="hash",
        workspace_id="workspace-one",
        pool=MachinePool("pool-one"),
        machine_id="machine-one",
    )


def _route(
    *,
    transport: BackendRouteTransport = BackendRouteTransport.TsnetRestricted,
) -> AgentBackendRoute:
    return AgentBackendRoute(
        route_id="route-one",
        workspace_id="workspace-one",
        pool=MachinePool("pool-one"),
        machine_id="machine-one",
        worker_id="worker-one",
        container_id="container-one",
        state=BackendRouteState.Ready,
        proxy_target="agent.tailnet:29443",
        transport=transport,
    )
