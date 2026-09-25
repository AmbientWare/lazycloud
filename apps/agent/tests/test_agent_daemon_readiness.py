from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
from agent.operations import (
    WORKER_ADMISSION_HOLD_ENV,
    AgentBootstrap,
    AgentCapacityInterruptionNotice,
    AgentState,
    AgentWorkerContainerPlan,
    AgentWorkerSlot,
    WorkerExecutor,
)
from agent.tunnel import AgentTunnelRoute, AgentTunnelService
from agent_app import daemon
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
    AgentReserveInstruction,
    AgentWorkerSlotStatus,
    agent_machine_worker_id,
)
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
from shared.placement import Placement
from shared.usage import UsageBillingOwner
from worker.network_backend import AgentBridgeCallbackFirewall, AgentBridgeNetworkConfig

from gateway import http


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

    def reset_connections(self) -> None:
        pass


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
    executor: WorkerExecutor = WorkerExecutor.External,
) -> AgentDaemonService:
    state_store = AgentStateStore(state_dir)
    state_store.save(
        AgentState(
            gateway_url="https://control.example.com",
            workspace_id="11111111-1111-4111-8111-111111111111",
            placement=Placement.machine("pool-one"),
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
            executor=executor,
            os_name="linux",
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


def test_interruption_notice_arms_its_deadline_and_a_later_notice_preempts(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    service = _service(
        tmp_path,
        _InterruptionGateway(events),
        worker_controller=_InterruptionWorkerController(tmp_path, events),
    )
    state = service.state_store.load(service.options.gateway_url)
    assert state is not None
    try:
        deadline = datetime.now(UTC) + timedelta(minutes=2)
        draining = service._begin_capacity_interruption(
            state, AgentCapacityInterruptionNotice(reason="provider-reclaim", notice_at=deadline)
        )
        assert draining.capacity_state is AgentCapacityState.Draining
        assert service._capacity_shutdown.deadline == deadline
        immediate = service._begin_capacity_interruption(
            draining, AgentCapacityInterruptionNotice(reason="provider-hibernate")
        )
        assert immediate.capacity_state is AgentCapacityState.Preempting
        assert immediate.capacity_notice_at == deadline
    finally:
        service._capacity_shutdown.close()
        service.worker_controller.close()


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


_MACHINE_WORKER = agent_machine_worker_id("machine-one")


def _machine_slot(status: AgentWorkerSlotStatus) -> http.AgentWorkerSlot:
    return http.AgentWorkerSlot(
        worker_id=_MACHINE_WORKER,
        worker_token="worker-secret",
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        billing_owner=UsageBillingOwner.PlatformFleet,
        machine_id="machine-one",
        worker_image="registry.example/worker:prepared",
        status=status,
    )


class _SlotGateway(_Gateway):
    """Answers each stream with the machine's worker slot and reserve instruction.

    It records whether each stream said the machine booted since its reserve
    was prepared.
    """

    def __init__(self) -> None:
        super().__init__()
        self.slot = _machine_slot(AgentWorkerSlotStatus.Active)
        self.reserve = AgentReserveInstruction.Prepare
        self.booted_since_prepared: list[bool] = []

    def stream_agent(self, request: StreamAgentRequest) -> StreamAgentResponse:
        self.booted_since_prepared.append(request.booted_since_reserve_prepared)
        return StreamAgentResponse(
            ok=True,
            credential_id="22222222-2222-4222-8222-222222222222",
            credential_generation=1,
            slots=[self.slot],
            reserve=self.reserve,
        )


class _Workers(DockerAgentWorkerController):
    """Docker as the running containers, each numbered in the order it started."""

    def __init__(self, state_dir: Path) -> None:
        super().__init__(state_dir)
        self.running: dict[str, tuple[AgentWorkerSlot, int]] = {}
        self.held: set[str] = set()
        self.started = 0

    def wait_for_docker(self, stop: Event | None = None) -> bool:
        del stop
        return True

    def _image_present(self, image: str, stop: Event | None = None) -> bool:
        del image, stop
        return True

    def _prepare_worker_image(self, image: str, stop: Event) -> None:
        del image, stop

    def _run_container(self, plan: AgentWorkerContainerPlan, *, stop: Event | None) -> None:
        del stop
        self.started += 1
        self.running[plan.slot.worker_id] = (plan.slot, self.started)
        if WORKER_ADMISSION_HOLD_ENV in plan.env:
            self.held.add(plan.slot.worker_id)
        else:
            self.held.discard(plan.slot.worker_id)

    def _stop(self, slot: AgentWorkerSlot) -> None:
        self.running.pop(slot.worker_id, None)
        self.held.discard(slot.worker_id)

    def holds(self, worker_id: str) -> bool:
        return worker_id in self.held

    def _running_slot(self, slot: AgentWorkerSlot) -> AgentWorkerSlot | None:
        return slot.model_copy() if slot.worker_id in self.running else None

    def containers(self) -> dict[str, int]:
        return {worker_id: started for worker_id, (_, started) in self.running.items()}

    def _reap_forgotten_workers(self, active_worker_ids: set[str]) -> None:
        del active_worker_ids


class _Tunnel(AgentTunnelService):
    """A connected tunnel that records the containers running when its listeners open."""

    workers: _Workers
    opened_with: dict[str, int] | None

    def reconcile_listeners(self, hosts: set[str]) -> None:
        del hosts
        self.opened_with = self.workers.containers()

    def hold_listeners(self) -> None:
        self.opened_with = None

    def reconcile_routes(self, routes: Sequence[AgentTunnelRoute]) -> set[str]:
        del routes
        return set()


@pytest.mark.parametrize(
    "resumed_slot", [AgentWorkerSlotStatus.Active, AgentWorkerSlotStatus.Draining]
)
def test_a_reserve_worker_reaches_the_control_plane_only_once_a_stream_adopts_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resumed_slot: AgentWorkerSlotStatus,
) -> None:
    """A hibernating reserve runs its worker while it is prepared, listeners closed.

    The first stream after the resume keeps that same container if it wants the
    slot active, and stops it otherwise, before the listeners open.
    """
    monkeypatch.setattr(daemon, "BOOT_ID_PATH", tmp_path / "boot_id")
    (tmp_path / "boot_id").write_text("reserve-boot")
    gateway = _SlotGateway()
    workers = _Workers(tmp_path / "agent")
    service = _service(
        tmp_path / "agent", gateway, worker_controller=workers, executor=WorkerExecutor.Container
    )
    state = service.state_store.load(service.options.gateway_url)
    assert state is not None
    tunnel = _Tunnel(
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
    tunnel.workers = workers
    tunnel.opened_with = None
    try:
        service.run_stream_iteration(state, tunnel=tunnel, before_agent_update=tunnel.close)
        service.run_stream_iteration(state, tunnel=tunnel, before_agent_update=tunnel.close)
        prepared = workers.containers()
        assert set(prepared) == {_MACHINE_WORKER}
        assert tunnel.opened_with is None
        assert workers.reserve_worker(state.machine_id) is not None
        gateway.reserve = AgentReserveInstruction.Serve
        gateway.slot = _machine_slot(resumed_slot)
        service.run_stream_iteration(state, tunnel=tunnel, before_agent_update=tunnel.close)
    finally:
        tunnel.close()
        workers.close()
        service._capacity_shutdown.close()

    assert tunnel.opened_with == (prepared if resumed_slot is AgentWorkerSlotStatus.Active else {})
    assert workers.containers() == tunnel.opened_with
    assert workers.reserve_worker(state.machine_id) is None


def test_a_cold_resumed_reserve_keeps_its_worker_until_its_row_catches_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reserve started again before its row reads resuming keeps the worker it booted.

    Every stream until the resume says the machine booted since its reserve was
    prepared, so the control plane keeps treating the stale row as a resume and
    the boot's worker is adopted, not stopped.
    """
    boot_id = tmp_path / "boot_id"
    monkeypatch.setattr(daemon, "BOOT_ID_PATH", boot_id)
    boot_id.write_text("prepared-boot")
    gateway = _SlotGateway()
    prepared = _Workers(tmp_path / "agent")
    service = _service(
        tmp_path / "agent", gateway, worker_controller=prepared, executor=WorkerExecutor.Container
    )
    state = service.state_store.load(service.options.gateway_url)
    assert state is not None
    tunnel = _Tunnel(
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
    tunnel.opened_with = None
    try:
        tunnel.workers = prepared
        service.run_stream_iteration(state, tunnel=tunnel, before_agent_update=tunnel.close)
        prepared.close()
        service._capacity_shutdown.close()

        boot_id.write_text("resumed-boot")
        resumed = _Workers(tmp_path / "agent")
        service = _service(
            tmp_path / "agent",
            gateway,
            worker_controller=resumed,
            executor=WorkerExecutor.Container,
        )
        tunnel.workers = resumed
        service._unconfirmed = service._prepare_boot_worker(
            service._reserve_worker(state), state.bootstrap
        )
        booted = resumed.containers()
        assert set(booted) == {_MACHINE_WORKER}
        gateway.booted_since_prepared.clear()
        gateway.reserve = AgentReserveInstruction.ResumePending
        for _ in range(2):
            service.run_stream_iteration(state, tunnel=tunnel, before_agent_update=tunnel.close)
            assert resumed.containers() == booted
            assert tunnel.opened_with is None
        gateway.reserve = AgentReserveInstruction.Serve
        service.run_stream_iteration(state, tunnel=tunnel, before_agent_update=tunnel.close)
    finally:
        tunnel.close()
        tunnel.workers.close()
        service._capacity_shutdown.close()

    assert gateway.booted_since_prepared == [True, True, True]
    assert tunnel.opened_with == booted
    assert resumed.reserve_worker(state.machine_id) is None
