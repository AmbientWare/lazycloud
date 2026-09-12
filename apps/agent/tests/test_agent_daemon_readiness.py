from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agent.operations import (
    AgentBootstrap,
    AgentCapacityInterruptionNotice,
    AgentState,
    WorkerExecutor,
)
from agent.tunnel import AgentTunnelService
from agent_app.daemon import (
    AgentDaemonOptions,
    AgentDaemonService,
    AgentStateStore,
    AgentStreamRetryableError,
    DockerAgentWorkerController,
    _recoverable_stream_error,
    build_agent_daemon_service,
)
from gateway.http import (
    AgentTelemetryRequest,
    AgentTelemetryResponse,
    JoinAgentRequest,
    JoinAgentResponse,
    LeaveAgentRequest,
    LeaveAgentResponse,
    ListAgentRoutesRequest,
    ListAgentRoutesResponse,
    StreamAgentRequest,
    StreamAgentResponse,
    UpdateAgentRouteStatusRequest,
    UpdateAgentRouteStatusResponse,
)
from shared.compute_enrollment import (
    AgentCapacityState,
)
from shared.compute_policy import MachinePool
from shared.http.agent_identity import (
    AgentCertificateRequest,
    AgentCertificateResponse,
    AgentTunnelIdentity,
)
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
from shared.http.releases import AgentReleaseRequest, AgentReleaseResponse
from worker.network_backend import AgentBridgeCallbackFirewall, AgentBridgeNetworkConfig


class _Gateway:
    def agent_release(self, request: AgentReleaseRequest) -> AgentReleaseResponse:
        return AgentReleaseResponse(generation=request.generation)

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
            credential_id="22222222-2222-4222-8222-222222222222",
            credential_generation=1,
        )

    def list_agent_routes(self, request: ListAgentRoutesRequest) -> ListAgentRoutesResponse:
        raise AssertionError("This durable transition must not start a network session")

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

    def issue_agent_certificate(self, request: AgentCertificateRequest) -> AgentCertificateResponse:
        raise AssertionError("This durable transition must not issue a network credential")

    def stream_agent_telemetry(
        self,
        request: AgentTelemetryRequest,
    ) -> AgentTelemetryResponse:
        del request
        return AgentTelemetryResponse(ok=True)


class _InterruptionGateway(_Gateway):
    def __init__(self, events: list[str], *, unavailable: bool = False) -> None:
        super().__init__()
        self.events = events
        self.unavailable = unavailable
        self.streams = 0

    def stream_agent(self, request: StreamAgentRequest) -> StreamAgentResponse:
        del request
        self.streams += 1
        return StreamAgentResponse(
            ok=True,
            credential_id="22222222-2222-4222-8222-222222222222",
            credential_generation=3,
            capacity_state=AgentCapacityState.Draining,
        )

    def record_agent_capacity_interruption(
        self,
        request: AgentCapacityInterruptionRequest,
    ) -> AgentCapacityInterruptionResponse:
        self.events.append(request.state.value)
        if self.unavailable:
            raise HttpApiError("gateway unavailable", status_code=503)
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

    def stop_all(self) -> None:
        self.events.append("workers:forced")


def _service(
    state_dir: Path,
    gateway: _Gateway,
    *,
    worker_controller: DockerAgentWorkerController | None = None,
) -> AgentDaemonService:
    state_store = AgentStateStore(state_dir)
    state_store.save(
        AgentState(
            gateway_url="https://control.example.com",
            workspace_id="11111111-1111-4111-8111-111111111111",
            pool=MachinePool("pool-one"),
            machine_id="machine-one",
            agent_token="agent-secret",
            credential_id="22222222-2222-4222-8222-222222222222",
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
        worker_controller=worker_controller or DockerAgentWorkerController(state_dir),
    )


def test_stale_release_cannot_apply_worker_instructions(tmp_path: Path) -> None:
    service = _service(tmp_path, _Gateway())
    state = service.state_store.load(service.options.gateway_url)
    assert state is not None
    service.state_store.save(state.model_copy(update={"release_generation": 2}))

    tunnel = AgentTunnelService(
        state_dir=tmp_path,
        identity=AgentTunnelIdentity(
            workspace_id=state.workspace_id,
            enrollment_id=state.credential_id,
            credential_generation=state.credential_generation,
        ),
        agent_token=state.agent_token,
        issue_certificate=service.client.issue_agent_certificate,
        callback_firewall=AgentBridgeCallbackFirewall(
            config=AgentBridgeNetworkConfig(), owner_id=state.machine_id
        ),
    )
    try:
        with pytest.raises(AgentStreamRetryableError, match="instruction is stale"):
            service.run_stream_iteration(
                state.model_copy(update={"release_generation": 2}),
                tunnel=tunnel,
                before_agent_update=tunnel.close,
            )
    finally:
        tunnel.close()
        service.worker_controller.close()
        service._capacity_shutdown.close()

    saved = service.state_store.load(service.options.gateway_url)
    assert saved is not None and saved.release_generation == 2
    assert not service.worker_controller.active_slots_path.exists()
    assert not service.state_store.ready_path.exists()


def test_expired_interruption_stops_workers_when_gateway_is_unavailable(tmp_path: Path) -> None:
    events: list[str] = []
    gateway = _InterruptionGateway(events, unavailable=True)
    service = _service(
        tmp_path, gateway, worker_controller=_InterruptionWorkerController(tmp_path, events)
    )
    state = service.state_store.load(service.options.gateway_url)
    assert state is not None
    interrupted = service._begin_capacity_interruption(
        state,
        AgentCapacityInterruptionNotice(
            reason="provider-capacity-reclaimed", notice_at=datetime.now(UTC) - timedelta(seconds=1)
        ),
    )
    try:
        result = service._resume_capacity_interruption(
            interrupted, current_iterations=1, tunnel_connected=False, runtime_http_url=""
        )
    finally:
        service._capacity_shutdown.close()
        service.worker_controller.close()

    assert result.capacity_interrupted
    assert result.capacity_state is AgentCapacityState.Cordoned
    assert events.count("workers:forced") == 1
    saved = service.state_store.load(service.options.gateway_url)
    assert saved is not None
    assert saved.capacity_state is AgentCapacityState.Cordoned


def test_transport_failures_are_recoverable_so_a_machine_keeps_rejoining() -> None:
    """A pre-response failure must not be fatal.

    A gateway TLS reset raises HttpTransportError, a plain RuntimeError. When that
    read as fatal the agent exited, systemd stopped restarting it, and the machine
    billed with no agent until someone noticed.
    """
    transport_failure = HttpTransportError(
        "POST",
        "https://gateway.example.com/gateway/agents/certificate",
        "EOF occurred in violation of protocol (_ssl.c:1010)",
    )
    assert _recoverable_stream_error(transport_failure)
    assert _recoverable_stream_error(HttpApiError("upstream", status_code=503))
    assert not _recoverable_stream_error(HttpApiError("forbidden", status_code=403))
