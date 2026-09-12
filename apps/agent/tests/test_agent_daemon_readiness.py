from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    AgentStreamRetryableError,
    DockerAgentWorkerController,
    ProviderInstanceIdentityMode,
    WorkerImagePullError,
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
    StreamAgentRequest,
    StreamAgentResponse,
    UpdateAgentRouteStatusRequest,
    UpdateAgentRouteStatusResponse,
)
from provider_clients import ProviderNodeIdentityEvidence
from pydantic import SecretStr
from shared.compute_enrollment import (
    AgentCapacityState,
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
)
from shared.compute_policy import MachinePool
from shared.http.errors import HttpApiError, HttpTransportError
from shared.http.gateway import (
    AgentCapacityInterruptionRequest,
    AgentCapacityInterruptionResponse,
)
from shared.http.private_network import (
    PrivateNetworkTopologyRequest,
    RegisterPrivateNetworkRequest,
    WireGuardPeerConfiguration,
)
from shared.http.provider_nodes import (
    ProviderNodeBootstrapFailureRequest,
    ProviderNodeBootstrapFailureResponse,
    ProviderNodeBootstrapPhaseRequest,
    ProviderNodeEnrollmentRequest,
)
from shared.http.releases import AgentReleaseRequest, AgentReleaseResponse
from shared.provider_config import ProviderKind


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
        request: RegisterPrivateNetworkRequest,
    ) -> WireGuardPeerConfiguration:
        del request
        raise AssertionError("direct transport should not register a private-network site")

    def private_network_topology(
        self,
        request: PrivateNetworkTopologyRequest,
    ) -> WireGuardPeerConfiguration:
        del request
        raise AssertionError("direct transport should not request private-network topology")

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
            credential_id="credential-one",
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


class _BootstrapGateway(_Gateway):
    def __init__(self) -> None:
        super().__init__()
        self.failures: list[ProviderNodeBootstrapFailureRequest] = []

    def record_provider_node_bootstrap_failure(
        self,
        request: ProviderNodeBootstrapFailureRequest,
    ) -> ProviderNodeBootstrapFailureResponse:
        self.failures.append(request)
        return ProviderNodeBootstrapFailureResponse(
            provider_instance_id=request.provider_instance_id,
            phase=MachineBootstrapPhase.Failed,
            failure_reason=request.failure_reason,
            observed_at=datetime.now(UTC),
        )

    def record_provider_node_bootstrap_phase(
        self,
        request: ProviderNodeBootstrapPhaseRequest,
    ) -> ProviderNodeBootstrapFailureResponse:
        return ProviderNodeBootstrapFailureResponse(
            provider_instance_id=request.provider_instance_id,
            phase=request.phase,
            observed_at=datetime.now(UTC),
        )


class _ProviderIdentity:
    def acknowledge(self) -> None:
        pass

    def create(self, *, expected_region: str | None = None) -> ProviderNodeIdentityEvidence:
        del expected_region
        return ProviderNodeIdentityEvidence(
            provider=ProviderKind.Aws,
            region="us-east-1",
            provider_instance_id="i-0123456789abcdef0",
            proof_url=SecretStr("https://identity.example.test/proof"),
        )


class _FailingImageController(DockerAgentWorkerController):
    def prepare_worker_image(self) -> None:
        raise WorkerImagePullError("registry unavailable")


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
            route_proxy=AgentRouteProxyConfig(bind_port=0),
            once=True,
        ),
        client=gateway,
        worker_controller=worker_controller or DockerAgentWorkerController(state_dir),
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


def test_stale_release_cannot_apply_worker_instructions(tmp_path: Path) -> None:
    service = _service(tmp_path, _Gateway())
    state = service.state_store.load(service.options.gateway_url)
    assert state is not None
    service.state_store.save(state.model_copy(update={"release_generation": 2}))

    with pytest.raises(AgentStreamRetryableError, match="instruction is stale"):
        service.run()

    saved = service.state_store.load(service.options.gateway_url)
    assert saved is not None and saved.release_generation == 2
    assert not service.worker_controller.active_slots_path.exists()
    assert not service.state_store.ready_path.exists()


def test_worker_image_pull_failure_is_reported_before_runtime_ready(tmp_path: Path) -> None:
    gateway = _BootstrapGateway()
    state_store = AgentStateStore(tmp_path)
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
    service = build_agent_daemon_service(
        AgentDaemonOptions(
            gateway_url="https://control.example.com",
            provider_enrollment_request="11111111-1111-4111-8111-111111111111",
            provider=ProviderKind.Aws,
            provider_instance_identity=ProviderInstanceIdentityMode.ImdsV2,
            state_dir=str(tmp_path),
            executor=WorkerExecutor.External,
            once=True,
        ),
        client=gateway,
        worker_controller=_FailingImageController(tmp_path),
        provider_identity=_ProviderIdentity(),
    )

    with pytest.raises(WorkerImagePullError, match="registry unavailable"):
        service.run()

    assert not state_store.ready_path.exists()
    assert [failure.failure_reason for failure in gateway.failures] == [
        MachineBootstrapFailureReason.WorkerImagePullFailed
    ]


@pytest.mark.parametrize("resume", [False, True])
def test_daemon_keeps_draining_workers_alive_until_shutdown_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resume: bool,
) -> None:
    current = [datetime(2026, 7, 21, 15, 28, tzinfo=UTC)]
    deadline = current[0] + timedelta(seconds=120)

    def advance(seconds: float) -> None:
        current[0] += timedelta(seconds=seconds)

    monkeypatch.setattr("agent_app.daemon.utc_now", lambda: current[0])
    monkeypatch.setattr("agent.capacity_shutdown.utc_now", lambda: current[0])
    monkeypatch.setattr("agent_app.daemon.time.sleep", advance)
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
            capacity_state=AgentCapacityState.Draining if resume else AgentCapacityState.Available,
            capacity_notice_at=deadline if resume else None,
            capacity_reason="provider-capacity-reclaimed" if resume else "",
            capacity_observed_at=current[0] if resume else None,
            bootstrap=AgentBootstrap(gateway_public_http_url="https://control.example.com"),
        )
    )
    service = build_agent_daemon_service(
        AgentDaemonOptions(
            gateway_url="https://control.example.com",
            state_dir=str(tmp_path),
            join_token="consumed-join-token" if resume else "",
            executor=WorkerExecutor.External,
            stream_interval_seconds=50,
            route_proxy=AgentRouteProxyConfig(bind_port=0),
        ),
        client=gateway,
        worker_controller=_InterruptionWorkerController(tmp_path, events),
        interruption_detector=lambda: (
            None
            if resume
            else AgentCapacityInterruptionNotice(
                reason="provider-capacity-reclaimed", observed_at=current[0], notice_at=deadline
            )
        ),
    )

    result = service.run()

    assert gateway.streams == 2
    assert events.count("draining") == 1
    assert [event for event in events if event.startswith("workers:")] == ["workers:15"]
    assert current[0] == deadline - timedelta(seconds=20)
    assert result.capacity_interrupted
    assert result.capacity_state is AgentCapacityState.Cordoned
    saved = state_store.load("https://control.example.com")
    assert saved is not None
    assert saved.capacity_state is AgentCapacityState.Cordoned
    assert saved.capacity_reason == "provider-capacity-reclaimed"


def test_expired_interruption_stops_workers_when_gateway_is_unavailable(tmp_path: Path) -> None:
    events: list[str] = []
    gateway = _InterruptionGateway(events, unavailable=True)
    service = _service(
        tmp_path, gateway, worker_controller=_InterruptionWorkerController(tmp_path, events)
    )
    service.interruption_detector = lambda: AgentCapacityInterruptionNotice(
        reason="provider-capacity-reclaimed", notice_at=datetime.now(UTC) - timedelta(seconds=1)
    )

    result = service.run()

    assert result.capacity_interrupted
    assert result.capacity_state is AgentCapacityState.Cordoned
    assert gateway.streams == 0
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
        "https://gateway.example.com/gateway/agents/private-network/register",
        "EOF occurred in violation of protocol (_ssl.c:1010)",
    )
    assert _recoverable_stream_error(transport_failure)
    assert _recoverable_stream_error(HttpApiError("upstream", status_code=503))
    assert not _recoverable_stream_error(HttpApiError("forbidden", status_code=403))
