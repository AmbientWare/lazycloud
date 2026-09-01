from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent.operations import (
    AgentBootstrap,
    AgentCapacityInterruptionNotice,
    AgentRuntimeReady,
    AgentState,
    WorkerExecutor,
)
from agent_app.daemon import (
    AgentDaemonOptions,
    AgentDaemonService,
    AgentStateStore,
    DockerAgentWorkerController,
    _recoverable_stream_error,
    build_agent_daemon_service,
)
from agent_app.route_proxy import AgentRouteProxyConfig
from gateway.http import (
    AgentTelemetryRequest,
    AgentTelemetryResponse,
    JoinAgentRequest,
    JoinAgentResponse,
    LeaveAgentRequest,
    LeaveAgentResponse,
    RegisterAgentPrivateNetworkRequest,
    RegisterAgentPrivateNetworkResponse,
    StreamAgentRequest,
    StreamAgentResponse,
    UpdateAgentRouteStatusRequest,
    UpdateAgentRouteStatusResponse,
)
from shared.compute_enrollment import AgentCapacityState
from shared.compute_policy import MachinePool
from shared.http.errors import HttpApiError, HttpTransportError
from shared.http.gateway import (
    AgentCapacityInterruptionRequest,
    AgentCapacityInterruptionResponse,
)
from shared.http.provider_nodes import (
    ProviderNodeBootstrapFailureRequest,
    ProviderNodeBootstrapFailureResponse,
    ProviderNodeBootstrapPhaseRequest,
    ProviderNodeEnrollmentRequest,
)


class _Gateway:
    def __init__(self, *, stream_error: Exception | None = None) -> None:
        self.stream_error = stream_error

    def join_agent(self, request: JoinAgentRequest) -> JoinAgentResponse:
        del request
        raise AssertionError("saved identity should be reused")

    def enroll_provider_node(self, request: ProviderNodeEnrollmentRequest) -> JoinAgentResponse:
        del request
        raise AssertionError("saved identity should be reused")

    def record_provider_node_bootstrap_failure(
        self,
        request: ProviderNodeBootstrapFailureRequest,
    ) -> ProviderNodeBootstrapFailureResponse:
        del request
        raise AssertionError("saved identity should not report bootstrap failure")

    def record_provider_node_bootstrap_phase(
        self,
        request: ProviderNodeBootstrapPhaseRequest,
    ) -> ProviderNodeBootstrapFailureResponse:
        del request
        raise AssertionError("saved identity should not report a bootstrap phase")

    def leave_agent(self, request: LeaveAgentRequest) -> LeaveAgentResponse:
        del request
        return LeaveAgentResponse(machine_id="machine-one")

    def stream_agent(self, request: StreamAgentRequest) -> StreamAgentResponse:
        del request
        if self.stream_error is not None:
            raise self.stream_error
        return StreamAgentResponse(
            ok=True,
            credential_id="credential-one",
            credential_generation=1,
        )

    def record_agent_capacity_interruption(
        self,
        request: AgentCapacityInterruptionRequest,
    ) -> AgentCapacityInterruptionResponse:
        raise AssertionError(f"unexpected capacity interruption: {request.state.value}")

    def update_agent_route_status(
        self,
        request: UpdateAgentRouteStatusRequest,
    ) -> UpdateAgentRouteStatusResponse:
        del request
        return UpdateAgentRouteStatusResponse()

    def register_agent_private_network(
        self,
        request: RegisterAgentPrivateNetworkRequest,
    ) -> RegisterAgentPrivateNetworkResponse:
        del request
        raise AssertionError("direct transport should not register a private-network site")

    def stream_agent_telemetry(
        self,
        request: AgentTelemetryRequest,
    ) -> AgentTelemetryResponse:
        del request
        return AgentTelemetryResponse(ok=True)


class _InterruptionGateway(_Gateway):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def stream_agent(self, request: StreamAgentRequest) -> StreamAgentResponse:
        del request
        raise AssertionError("capacity must be cordoned before the next agent stream")

    def record_agent_capacity_interruption(
        self,
        request: AgentCapacityInterruptionRequest,
    ) -> AgentCapacityInterruptionResponse:
        self.events.append(request.state.value)
        return AgentCapacityInterruptionResponse(
            machine_id=request.machine_id,
            credential_id=request.credential_id,
            credential_generation=request.credential_generation,
            state=request.state,
            reason=request.reason,
            observed_at=request.observed_at,
            notice_at=request.notice_at,
            changed=True,
        )


class _InterruptionWorkerController(DockerAgentWorkerController):
    def __init__(self, state_dir: Path, events: list[str]) -> None:
        super().__init__(state_dir)
        self.events = events

    def gracefully_stop_all(self, *, grace_seconds: float) -> None:
        self.events.append(f"workers:{grace_seconds:g}")


def _service(state_dir: Path, gateway: _Gateway) -> AgentDaemonService:
    state_store = AgentStateStore(state_dir)
    state_store.save(
        AgentState(
            gateway_url="https://control.example.com",
            workspace_id="workspace-one",
            pool=MachinePool("pool-one"),
            machine_id="machine-one",
            agent_token="agent-secret",
            credential_id="credential-one",
            credential_generation=1,
            bootstrap=AgentBootstrap(gateway_public_http_url="https://control.example.com"),
        )
    )
    return build_agent_daemon_service(
        AgentDaemonOptions(
            gateway_url="https://control.example.com",
            state_dir=str(state_dir),
            executor=WorkerExecutor.External,
            once=True,
        ),
        client=gateway,
        worker_controller=DockerAgentWorkerController(state_dir),
    )


def test_daemon_writes_runtime_ready_marker_after_first_successful_stream(tmp_path: Path) -> None:
    service = _service(tmp_path, _Gateway())

    result = service.run()

    marker = AgentRuntimeReady.model_validate_json(service.state_store.ready_path.read_text())
    assert result.stream_iterations == 1
    assert marker.machine_id == "machine-one"
    assert marker.stream_iteration == 1
    assert service.state_store.ready_path.stat().st_mode & 0o777 == 0o600
    assert "agent-secret" not in service.state_store.ready_path.read_text()


def test_daemon_removes_stale_ready_marker_before_failed_stream(tmp_path: Path) -> None:
    service = _service(tmp_path, _Gateway(stream_error=ConnectionResetError("gateway reset")))
    service.state_store.ready_path.write_text("stale", encoding="utf-8")

    with pytest.raises(ConnectionResetError, match="gateway reset"):
        service.run()

    assert not service.state_store.ready_path.exists()


def test_daemon_cordons_current_session_before_bounded_worker_shutdown(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    gateway = _InterruptionGateway(events)
    state_store = AgentStateStore(tmp_path)
    state_store.save(
        AgentState(
            gateway_url="https://control.example.com",
            workspace_id="workspace-one",
            pool=MachinePool("pool-one"),
            machine_id="machine-one",
            agent_token="agent-secret",
            credential_id="credential-one",
            credential_generation=3,
            bootstrap=AgentBootstrap(gateway_public_http_url="https://control.example.com"),
        )
    )
    service = build_agent_daemon_service(
        AgentDaemonOptions(
            gateway_url="https://control.example.com",
            state_dir=str(tmp_path),
            executor=WorkerExecutor.External,
            interruption_grace_seconds=90,
            route_proxy=AgentRouteProxyConfig(bind_port=0),
        ),
        client=gateway,
        worker_controller=_InterruptionWorkerController(tmp_path, events),
        interruption_detector=lambda: AgentCapacityInterruptionNotice(
            reason="aws-ec2-spot-terminate",
            observed_at=datetime(2026, 7, 21, 15, 28, tzinfo=UTC),
            notice_at=datetime(2026, 7, 21, 15, 30, tzinfo=UTC),
        ),
    )

    result = service.run()

    assert events == ["preempting", "cordoned", "workers:90"]
    assert result.capacity_interrupted
    assert result.capacity_state is AgentCapacityState.Cordoned
    saved = state_store.load("https://control.example.com")
    assert saved is not None
    assert saved.capacity_state is AgentCapacityState.Cordoned
    assert saved.capacity_reason == "aws-ec2-spot-terminate"


def test_transport_failures_are_recoverable_so_a_machine_keeps_rejoining() -> None:
    """A pre-response failure must not be fatal.

    A gateway TLS reset raises HttpTransportError, a plain RuntimeError. When that
    read as fatal the agent exited, systemd stopped restarting it, and the machine
    billed with no agent until someone noticed.
    """
    transport_failure = HttpTransportError(
        "POST",
        "https://gateway.example.com/gateway/agents/private-network",
        "EOF occurred in violation of protocol (_ssl.c:1010)",
    )
    assert _recoverable_stream_error(transport_failure)
    assert _recoverable_stream_error(HttpApiError("upstream", status_code=503))
    assert not _recoverable_stream_error(HttpApiError("forbidden", status_code=403))
