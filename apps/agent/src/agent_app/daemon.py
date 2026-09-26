from __future__ import annotations

import http.client as http_client
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import Thread
from types import TracebackType
from typing import Protocol

from agent.capacity_shutdown import DEFAULT_INTERRUPTION_GRACE_SECONDS, CapacityShutdown
from agent.operations import (
    AgentBootstrap,
    AgentCapacity,
    AgentCapacityCheck,
    AgentCapacityInterruptionNotice,
    AgentCapacityOptions,
    AgentDetectedResources,
    AgentGpuDevice,
    AgentResourceDetection,
    AgentState,
    AgentWorkerNetwork,
    AgentWorkerReconcileAction,
    AgentWorkerSlot,
    WorkerExecutor,
    WorkerSlotAction,
    normalize_gateway_url,
    parse_nvidia_smi_gpu_devices,
    plan_agent_lock,
    plan_worker_slot_reconciliation,
    resolve_agent_capacity,
    worker_slot_kept,
)
from agent.service_manager import (
    DEFAULT_AGENT_STATE_DIR,
    AgentPreflightProbeSet,
    PreflightCheckName,
    machine_fingerprint,
    plan_agent_preflight,
)
from agent.state import AgentStateStore, model_payload
from agent.storage_cleanup import (
    finish_stop_preparation,
    prepare_machine_storage_for_stop,
    read_stop_preparation,
)
from agent.suspend import SuspendWatch, Wakeup
from agent.tunnel import AgentTunnelRoute, AgentTunnelService
from agent.updates import AgentUpdateBlockedError, AgentUpdater
from agent.worker_controller import (
    DOCKER_WAIT_SECONDS,
    AgentReserveWorker,
    DockerAgentWorkerController,
    WorkerImagePullError,
)
from compute.provider_nodes import (
    ProviderNodeIdentityProofProvider,
    ProviderNodeIdentityUnavailableError,
)
from gateway.http import (
    AgentBootstrapConfig,
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
from networking.tunnel_agent import AgentTunnelRevokedError
from provider_aws.provider_node_interruption import (
    AwsEc2SpotInterruptionMonitor,
    AwsSpotInterruptionMonitorError,
)
from pydantic import Field, field_validator, model_validator
from shared.agent_connections import AGENT_TUNNEL_CONTROL_URL
from shared.compute_enrollment import (
    AgentCapacityState,
    AgentReserveInstruction,
    ComputePreflightCheck,
    MachineBootstrapFailureReason,
    agent_machine_worker_id,
)
from shared.compute_fleet import MachineLifecycle
from shared.contracts import ContractModel
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
    ProviderNodeCapacity,
    ProviderNodeEnrollmentRequest,
)
from shared.http.releases import (
    AGENT_RELEASE_GENERATION_HEADER,
    AgentReleaseRequest,
    AgentReleaseResponse,
)
from shared.http_transport import HttpChannel
from shared.provider_config import ProviderKind
from shared.routing import BackendRouteState
from shared.step_timings import StepTimings, seconds_since_boot
from shared.timestamps import utc_now
from worker.network_backend import AgentBridgeCallbackFirewall, AgentBridgeNetworkConfig

from agent_app.metrics import agent_metric_snapshot, physical_memory_mb
from agent_app.telemetry import (
    AgentTelemetryBuffer,
    AgentTelemetryEventType,
    AgentTelemetrySource,
    AgentTelemetryStream,
)
from gateway import http

LOGGER = logging.getLogger(__name__)

DEFAULT_AGENT_STREAM_INTERVAL_SECONDS = 5.0
SLOW_STREAM_ITERATION_SECONDS = 2.0
"""A stream iteration at least this long logs its step timings even when it changed nothing."""
DEFAULT_AGENT_HTTP_TIMEOUT_SECONDS = 30.0
NVIDIA_SMI_TIMEOUT_SECONDS = 5.0
JOIN_RETRY_SECONDS = 180.0
"""How long a startup step spends backing off between attempts before it gives up."""
JOIN_RETRY_BASE_SECONDS = 0.15
JOIN_RETRY_MAX_SECONDS = 30.0
IMAGE_REPORT_WAIT_SECONDS = 1.0
DEFAULT_MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
AGENT_AUTHORITY_REVOKED_DETAILS = frozenset(
    {
        "invalid agent token",
        "agent credential is no longer current",
        "agent capacity interruption session fence is stale",
        "agent token is no longer current",
        "agent machine no longer exists",
        "machine no longer exists",
    }
)
type AgentResourceDetector = Callable[[], AgentResourceDetection]
type AgentCapacityInterruptionDetector = Callable[[], AgentCapacityInterruptionNotice | None]


class ProviderInstanceIdentityMode(StrEnum):
    ImdsV2 = "imds-v2"


class AgentCapacityInterruptionDetectionError(RuntimeError):
    pass


class AgentStreamRetryableError(RuntimeError):
    pass


class AgentAuthorityRevokedError(RuntimeError):
    """Raised on startup when this machine's authority was already revoked.

    Terminal by construction: the process exits non-zero and the unit's start
    limit stops respawning it, rather than rejoining once every restart.
    """


class AgentDaemonOptions(ContractModel):
    gateway_url: str = "http://127.0.0.1:9000"
    join_token: str = ""
    join_token_file: str = ""
    provider_enrollment_request: str = ""
    provider: ProviderKind | None = None
    provider_instance_identity: ProviderInstanceIdentityMode | None = None
    state_dir: str = DEFAULT_AGENT_STATE_DIR
    machine_fingerprint: str = ""
    hostname: str = ""
    os_name: str = Field(default_factory=lambda: platform.system().lower())
    arch: str = Field(default_factory=platform.machine)
    executor: WorkerExecutor = WorkerExecutor.Container
    worker_image: str = ""
    worker_network: AgentWorkerNetwork = Field(default_factory=AgentWorkerNetwork)
    worker_host_aliases: list[str] = Field(default_factory=list)
    docker_binary: str = "docker"
    stream_interval_seconds: float = DEFAULT_AGENT_STREAM_INTERVAL_SECONDS
    http_timeout_seconds: float = DEFAULT_AGENT_HTTP_TIMEOUT_SECONDS
    interruption_grace_seconds: float = DEFAULT_INTERRUPTION_GRACE_SECONDS
    once: bool = False
    capacity: AgentCapacityOptions = Field(default_factory=AgentCapacityOptions)

    @field_validator(
        "stream_interval_seconds",
        "http_timeout_seconds",
        "interruption_grace_seconds",
    )
    @classmethod
    def intervals_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            msg = "agent daemon intervals must be positive"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def enrollment_method_must_be_complete(self) -> AgentDaemonOptions:
        provider_values = (
            bool(self.provider_enrollment_request),
            self.provider is not None,
            self.provider_instance_identity is not None,
        )
        if any(provider_values) and not all(provider_values):
            raise ValueError(
                "provider enrollment request, provider, and instance identity are required together"
            )
        if all(provider_values) and (self.join_token or self.join_token_file):
            raise ValueError("join credentials and provider enrollment cannot be combined")
        return self


class AgentDaemonRunResult(ContractModel):
    workspace_id: str = ""
    placement: str = ""
    machine_id: str = ""
    stream_iterations: int = 0
    route_count: int = 0
    desired_worker_count: int = 0
    slot_action_count: int = 0
    telemetry_sent: bool = False
    tunnel_connected: bool = False
    runtime_http_url: str = ""
    authority_revoked: bool = False
    capacity_state: AgentCapacityState = AgentCapacityState.Available
    capacity_interrupted: bool = False


class AgentMachineRegistration(ContractModel):
    machine_fingerprint: str
    hostname: str
    capacity: AgentCapacity
    preflight: list[ComputePreflightCheck] = Field(default_factory=list)
    schedulable: bool = True


class AgentLeaveClient(Protocol):
    def leave_agent(self, request: LeaveAgentRequest) -> LeaveAgentResponse: ...


class AgentGatewayClient(AgentLeaveClient, Protocol):
    def join_agent(self, request: JoinAgentRequest) -> JoinAgentResponse: ...

    def enroll_provider_node(
        self,
        request: ProviderNodeEnrollmentRequest,
    ) -> JoinAgentResponse: ...

    def record_provider_node_bootstrap_failure(
        self,
        request: ProviderNodeBootstrapFailureRequest,
    ) -> ProviderNodeBootstrapFailureResponse: ...

    def record_provider_node_bootstrap_phase(
        self,
        request: ProviderNodeBootstrapPhaseRequest,
    ) -> ProviderNodeBootstrapFailureResponse: ...

    def stream_agent(self, request: StreamAgentRequest) -> StreamAgentResponse: ...

    def list_agent_routes(self, request: ListAgentRoutesRequest) -> ListAgentRoutesResponse: ...

    def agent_release(self, request: AgentReleaseRequest) -> AgentReleaseResponse: ...

    def record_agent_capacity_interruption(
        self,
        request: AgentCapacityInterruptionRequest,
    ) -> AgentCapacityInterruptionResponse: ...

    def update_agent_route_status(
        self,
        request: UpdateAgentRouteStatusRequest,
    ) -> UpdateAgentRouteStatusResponse: ...

    def issue_agent_certificate(
        self, request: AgentCertificateRequest
    ) -> AgentCertificateResponse: ...

    def stream_agent_telemetry(
        self,
        request: AgentTelemetryRequest,
    ) -> AgentTelemetryResponse: ...

    def reset_connections(self) -> None: ...


@dataclass(slots=True)
class HttpAgentGatewayClient:
    channel: HttpChannel

    @classmethod
    def from_options(cls, options: AgentDaemonOptions) -> HttpAgentGatewayClient:
        return cls(
            HttpChannel(
                endpoint=options.gateway_url,
                timeout_seconds=options.http_timeout_seconds,
            )
        )

    def join_agent(self, request: JoinAgentRequest) -> JoinAgentResponse:
        return JoinAgentResponse.model_validate(
            self.channel.post("/gateway/agents/join", model_payload(request))
        )

    def list_agent_routes(self, request: ListAgentRoutesRequest) -> ListAgentRoutesResponse:
        return ListAgentRoutesResponse.model_validate(
            self.channel.post("/gateway/agents/routes", model_payload(request))
        )

    def enroll_provider_node(
        self,
        request: ProviderNodeEnrollmentRequest,
    ) -> JoinAgentResponse:
        return JoinAgentResponse.model_validate(
            self.channel.post("/gateway/provider-nodes/enroll", model_payload(request))
        )

    def record_provider_node_bootstrap_failure(
        self,
        request: ProviderNodeBootstrapFailureRequest,
    ) -> ProviderNodeBootstrapFailureResponse:
        return ProviderNodeBootstrapFailureResponse.model_validate(
            self.channel.post("/gateway/provider-nodes/bootstrap-failure", model_payload(request))
        )

    def record_provider_node_bootstrap_phase(
        self,
        request: ProviderNodeBootstrapPhaseRequest,
    ) -> ProviderNodeBootstrapFailureResponse:
        return ProviderNodeBootstrapFailureResponse.model_validate(
            self.channel.post("/gateway/provider-nodes/bootstrap-phase", model_payload(request))
        )

    def leave_agent(self, request: LeaveAgentRequest) -> LeaveAgentResponse:
        return LeaveAgentResponse.model_validate(
            self.channel.post("/gateway/agents/leave", model_payload(request))
        )

    def stream_agent(self, request: StreamAgentRequest) -> StreamAgentResponse:
        result = self.channel.request_response(
            "POST",
            "/gateway/agents/stream",
            payload=model_payload(request),
            headers={AGENT_RELEASE_GENERATION_HEADER: str(request.generation)},
        )
        generation = result.headers.get(AGENT_RELEASE_GENERATION_HEADER)
        if generation is None:
            raise AgentStreamRetryableError("agent stream has no release generation")
        try:
            response = StreamAgentResponse.model_validate(result.payload)
            return response.model_copy(update={"generation": int(generation)})
        except ValueError as exc:
            raise AgentStreamRetryableError("agent stream release generation is invalid") from exc

    def agent_release(self, request: AgentReleaseRequest) -> AgentReleaseResponse:
        return AgentReleaseResponse.model_validate(
            self.channel.post("/gateway/agents/release", model_payload(request))
        )

    def record_agent_capacity_interruption(
        self,
        request: AgentCapacityInterruptionRequest,
    ) -> AgentCapacityInterruptionResponse:
        return AgentCapacityInterruptionResponse.model_validate(
            self.channel.post("/gateway/agents/capacity-interruption", model_payload(request))
        )

    def update_agent_route_status(
        self,
        request: UpdateAgentRouteStatusRequest,
    ) -> UpdateAgentRouteStatusResponse:
        return UpdateAgentRouteStatusResponse.model_validate(
            self.channel.post("/gateway/agents/routes/status", model_payload(request))
        )

    def issue_agent_certificate(self, request: AgentCertificateRequest) -> AgentCertificateResponse:
        return AgentCertificateResponse.model_validate(
            self.channel.post("/gateway/agents/certificate", model_payload(request))
        )

    def stream_agent_telemetry(
        self,
        request: AgentTelemetryRequest,
    ) -> AgentTelemetryResponse:
        return AgentTelemetryResponse.model_validate(
            self.channel.post("/gateway/agents/telemetry", model_payload(request))
        )

    def reset_connections(self) -> None:
        self.channel.reset()


@dataclass(slots=True)
class AgentProcessLock:
    path: Path
    fd: int

    @classmethod
    def acquire(cls, state_dir: Path) -> AgentProcessLock:
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        plan = plan_agent_lock(str(state_dir), pid=os.getpid())
        lock_path = Path(plan.path)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, plan.permissions)
        except FileExistsError as exc:
            if _agent_lock_is_stale(lock_path, current_pid=os.getpid()):
                lock_path.unlink(missing_ok=True)
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, plan.permissions)
                os.write(fd, plan.contents.encode("utf-8"))
                return cls(path=lock_path, fd=fd)
            msg = f"agent lock already exists: {lock_path}"
            raise RuntimeError(msg) from exc
        os.write(fd, plan.contents.encode("utf-8"))
        return cls(path=lock_path, fd=fd)

    def release(self) -> None:
        os.close(self.fd)
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> AgentProcessLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc_value, traceback
        self.release()


@dataclass(slots=True)
class AgentDaemonService:
    options: AgentDaemonOptions
    client: AgentGatewayClient
    state_store: AgentStateStore
    worker_controller: DockerAgentWorkerController
    resource_detector: AgentResourceDetector | None = None
    interruption_detector: AgentCapacityInterruptionDetector | None = None
    telemetry: AgentTelemetryBuffer = field(default_factory=AgentTelemetryBuffer)
    provider_identity: ProviderNodeIdentityProofProvider | None = None
    _bootstrap_failure_reported: bool = False
    _capacity_shutdown: CapacityShutdown = field(init=False)
    _interruption_reported: bool = False
    _reported_worker_images: list[str] = field(default_factory=list)
    _last_applied_actions: str = "nothing"
    _identity_failure: MachineBootstrapFailureReason = (
        MachineBootstrapFailureReason.ProviderIdentityFailed
    )
    """What identity resolution reports once its retries run out: the stage it last reached."""
    _unconfirmed: AgentWorkerSlot | None = None
    """A reserve's worker, running before a stream has let it reach the control plane."""
    _holding: bool = False
    """Whether the worker listeners are held for a reserve. Only a stream that says
    serve releases them; one that says keep leaves them held."""
    _suspend: SuspendWatch = field(default_factory=SuspendWatch)
    _wakeup: Wakeup | None = field(default=None, init=False)
    """Open only while `run` runs; its pipe, timerfd and watch thread close with it."""

    def __post_init__(self) -> None:
        self._capacity_shutdown = CapacityShutdown(
            self.worker_controller, grace_seconds=self.options.interruption_grace_seconds
        )

    def _provider_evidence_provider(self) -> ProviderNodeIdentityProofProvider:
        if self.options.provider is None:
            msg = "provider node reporting requires a provider"
            raise ValueError(msg)
        return self.provider_identity or _provider_node_identity()

    def _report_phase_aside(self, phase: MachineLifecycle) -> None:
        """Report a boot phase on its own thread, so it never holds the tunnel or a stream.

        It retries within the join budget and logs a final failure; a machine
        that misses it still joins through its stream.
        """
        if not self.options.provider_enrollment_request:
            return

        def report() -> None:
            try:
                self._join_step(
                    f"bootstrap_phase.{phase.value}",
                    lambda: self._record_bootstrap_phase(phase),
                )
            except Exception as exc:
                if isinstance(exc, HttpApiError) and exc.status_code == 409:
                    # Enrollment moved the machine past this phase first.
                    LOGGER.info("the machine is already past %s", phase.value)
                    return
                LOGGER.warning("reporting the %s phase failed", phase.value, exc_info=True)

        Thread(target=report, name="agent-phase-report", daemon=True).start()

    def _record_bootstrap_phase(self, phase: MachineLifecycle) -> None:
        proof = self._provider_evidence_provider().create()
        self.client.record_provider_node_bootstrap_phase(
            ProviderNodeBootstrapPhaseRequest(
                enrollment_request_id=self.options.provider_enrollment_request,
                provider=proof.provider,
                region=proof.region,
                provider_instance_id=proof.provider_instance_id,
                identity_proof_url=proof.proof_url.get_secret_value(),
                phase=phase,
            )
        )

    def _report_bootstrap_failure(self, reason: MachineBootstrapFailureReason) -> None:
        if not self.options.provider_enrollment_request or self._bootstrap_failure_reported:
            return
        self._bootstrap_failure_reported = True
        # The active exception is the diagnosis; the report is the only way it
        # leaves a machine no one can reach.
        excerpt = traceback.format_exc()[-8192:]
        with suppress(Exception):
            provider = self._provider_evidence_provider()
            proof = provider.create()
            self.client.record_provider_node_bootstrap_failure(
                ProviderNodeBootstrapFailureRequest(
                    enrollment_request_id=self.options.provider_enrollment_request,
                    provider=proof.provider,
                    region=proof.region,
                    provider_instance_id=proof.provider_instance_id,
                    identity_proof_url=proof.proof_url.get_secret_value(),
                    failure_reason=reason,
                    diagnostic_excerpt=excerpt,
                )
            )

    def run(self) -> AgentDaemonRunResult:
        wakeup = Wakeup(
            on_resume=self.client.reset_connections, watch_resume=sys.platform == "linux"
        )
        self._wakeup = wakeup
        self.worker_controller.wake_on_image_prepared(wakeup.wake)
        try:
            return self._run()
        finally:
            self.worker_controller.wake_on_image_prepared(None)
            self._wakeup = None
            wakeup.close()

    def _run(self) -> AgentDaemonRunResult:
        LOGGER.info("agent started at boot+%.2fs", seconds_since_boot())
        self.state_store.begin_run()
        try:
            AgentUpdater.running(self.state_store.state_dir).prune()
        except Exception:
            LOGGER.warning("pruning old agent releases failed", exc_info=True)
        saved_state = self.state_store.load(self.options.gateway_url)
        if saved_state is not None and saved_state.capacity_notice_at is not None:
            self._capacity_shutdown.arm(saved_state.capacity_notice_at)
        try:
            if saved_state is None:
                # Before enrolling: the join records joining, and booting cannot follow it.
                self._report_phase_aside(MachineLifecycle.Booting)
                # Enrollment reports Docker in its preflight. Its wait stays out
                # of the network retry budget below.
                if self.options.executor is WorkerExecutor.Container:
                    self.worker_controller.wait_for_docker()
            state = self._join_step("identity.resolve", self.resolve_identity)
        except Exception:
            try:
                if self._capacity_shutdown.deadline is not None:
                    self._capacity_shutdown.stop()
            finally:
                self._capacity_shutdown.close()
                self._report_bootstrap_failure(self._identity_failure)
            raise
        if state.capacity_notice_at is not None:
            self._capacity_shutdown.arm(state.capacity_notice_at)
        reserve_worker = self._reserve_worker(state)
        self._holding = reserve_worker is not None
        iterations = 0
        last_result = AgentDaemonRunResult(
            workspace_id=state.workspace_id,
            placement=state.placement.key,
            machine_id=state.machine_id,
        )
        tunnel: AgentTunnelService | None = None
        runtime_ready = False

        def close_runtime() -> None:
            try:
                self._capacity_shutdown.close()
                if self._capacity_shutdown.deadline is not None:
                    self._capacity_shutdown.stop()
            finally:
                try:
                    if tunnel is not None:
                        tunnel.close()
                finally:
                    self.worker_controller.close()

        try:
            try:
                tunnel = self._join_step(
                    "tunnel.start",
                    lambda: self._start_tunnel(
                        state, hold_worker_control=reserve_worker is not None
                    ),
                )
            except Exception:
                self._report_bootstrap_failure(MachineBootstrapFailureReason.NetworkJoinFailed)
                raise
            LOGGER.info("agent tunnel connected at boot+%.2fs", seconds_since_boot())
            if saved_state is not None:
                # A restart or resume joined before; the report only moves a failed
                # machine back, so it waits for the network the tunnel proved.
                self._report_phase_aside(MachineLifecycle.Joining)
            self._unconfirmed = self._prepare_boot_worker(reserve_worker, state.bootstrap)
            last_result = last_result.model_copy(
                update={
                    "tunnel_connected": tunnel.connected,
                    "runtime_http_url": AGENT_TUNNEL_CONTROL_URL,
                }
            )
            while True:
                next_iteration = iterations + 1
                # A tunnel still down after a whole interval's wait has already
                # spent the retry's pause.
                waited_out = False
                try:
                    self._react_to_sleep(tunnel)
                    state = self.state_store.load(state.gateway_url) or state
                    notice = self._poll_capacity_interruption()
                    if notice is not None:
                        state = self._begin_capacity_interruption(state, notice)
                    if state.capacity_state is AgentCapacityState.Available:
                        waited_out = not tunnel.wait_connected(self.options.stream_interval_seconds)
                        tunnel.check()
                    last_result = self.run_stream_iteration(
                        state,
                        current_iterations=next_iteration,
                        tunnel=tunnel,
                        before_agent_update=close_runtime,
                        tunnel_connected=tunnel.connected,
                        runtime_http_url=AGENT_TUNNEL_CONTROL_URL,
                    )
                except Exception as exc:
                    if agent_authority_was_revoked(exc):
                        self._capacity_shutdown.stop(force=True)
                        self.state_store.mark_authority_revoked(state)
                        return last_result.model_copy(
                            update={
                                "stream_iterations": next_iteration,
                                "authority_revoked": True,
                            }
                        )
                    if (
                        isinstance(exc, WorkerImagePullError)
                        and not self.worker_controller.observe_workers()
                    ):
                        self._report_bootstrap_failure(
                            MachineBootstrapFailureReason.WorkerImagePullFailed
                        )
                        raise
                    if self.options.once or not _recoverable_stream_error(exc):
                        raise
                    LOGGER.warning("Agent stream failed; retrying", exc_info=True)
                    iterations = next_iteration
                    self.telemetry.enqueue_event(
                        event_type=AgentTelemetryEventType.Agent,
                        action="stream.retry",
                        status="retrying",
                        message="agent stream failed; retrying",
                        attrs={"error_type": type(exc).__name__},
                    )
                    if not waited_out:
                        self._sleep(self.options.stream_interval_seconds, tunnel)
                    continue
                if last_result.capacity_interrupted:
                    return last_result
                if not runtime_ready:
                    self.state_store.mark_ready(state, stream_iteration=next_iteration)
                    runtime_ready = True
                iterations = next_iteration
                if self.options.once:
                    return last_result
                self._sleep(self.options.stream_interval_seconds, tunnel, until_image=True)
        except Exception as exc:
            if not agent_authority_was_revoked(exc):
                raise
            self._capacity_shutdown.stop(force=True)
            self.state_store.mark_authority_revoked(state)
            return last_result.model_copy(update={"authority_revoked": True})
        finally:
            close_runtime()

    def _sleep(
        self, seconds: float, tunnel: AgentTunnelService, *, until_image: bool = False
    ) -> None:
        """Wait, ending early if the machine sleeps and resumes meanwhile."""
        wakeup = self._wakeup
        if wakeup is None:
            raise RuntimeError("the agent waits between streams only while it runs")
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            if until_image and self.worker_controller.has_unreported_image(
                self._reported_worker_images
            ):
                return
            wakeup.wait(remaining)
            if self._react_to_sleep(tunnel):
                return

    def _react_to_sleep(self, tunnel: AgentTunnelService) -> bool:
        """After the machine slept, drop its dead connections and restart its timers.

        Every remote connection died while it slept, and the monotonic clock stood
        still, so a timer set before the sleep fires late.
        """
        slept = self._suspend.slept()
        if not slept:
            return False
        LOGGER.info(
            "machine slept for %.1fs; redialing the tunnel at boot+%.2fs",
            slept,
            seconds_since_boot(),
        )
        tunnel.redial()
        self._capacity_shutdown.rearm()
        return True

    def _reserve_worker(self, state: AgentState) -> AgentReserveWorker | None:
        if (
            state.capacity_state is not AgentCapacityState.Available
            or state.capacity_notice_at is not None
            or self.options.executor is not WorkerExecutor.Container
            or agent_worker_reconcile_os(self.options.os_name) != "linux"
        ):
            return None
        return self.worker_controller.reserve_worker(state.machine_id)

    def _prepare_boot_worker(
        self, reserve_worker: AgentReserveWorker | None, bootstrap: AgentBootstrap
    ) -> AgentWorkerSlot | None:
        """Find the worker image, and for a reserve run its worker, before the first stream.

        The tunnel keeps that worker's listeners closed until a stream adopts or
        stops it, and a start that fails is left to the stream.
        """
        if self.options.executor is not WorkerExecutor.Container:
            return None
        self.worker_controller.prepare_worker_image()
        if reserve_worker is None:
            return None
        try:
            slot = self.worker_controller.start_reserve_worker(reserve_worker, bootstrap)
        except Exception:
            LOGGER.warning("starting the reserve's worker at boot failed", exc_info=True)
            return None
        if slot is not None:
            LOGGER.info(
                "reserve worker %s running at boot+%.2fs", slot.worker_id, seconds_since_boot()
            )
        return slot

    def _settle_unconfirmed_worker(
        self,
        worker: AgentWorkerSlot,
        desired_slots: list[AgentWorkerSlot],
        active_slots: list[AgentWorkerSlot],
        *,
        preparing: bool,
    ) -> list[AgentWorkerSlot]:
        """Keep a reserve's worker only where the planner would keep it running.

        Anything else, a draining or changed slot, another release, no slot at
        all, or a container that already exited, stops it here, and the plan
        then treats the slot as never started.
        """
        wanted = next((s for s in desired_slots if s.worker_id == worker.worker_id), None)
        running = next((s for s in active_slots if s.worker_id == worker.worker_id), None)
        if wanted is not None and worker_slot_kept(running, wanted):
            if not preparing:
                LOGGER.info(
                    "adopted reserve worker %s at boot+%.2fs",
                    worker.worker_id,
                    seconds_since_boot(),
                )
            return active_slots
        LOGGER.info(
            "stopping reserve worker %s: %s",
            worker.worker_id,
            "its container exited before a stream adopted it"
            if running is None
            else "the stream wants no slot"
            if wanted is None
            else f"the stream wants a {wanted.status.value} slot for {wanted.worker_image}",
        )
        self.worker_controller.stop_reserve_worker(worker)
        return [slot for slot in active_slots if slot.worker_id != worker.worker_id]

    def resolve_identity(self) -> AgentState:
        revoked = self.state_store.authority_revoked()
        if revoked is not None:
            # Nothing this process can do recovers a revoked authority, and the
            # control plane rejects every join it would attempt. Refusing here
            # is what makes the refusal cost one exit instead of one per restart
            # for the life of the machine.
            msg = (
                f"authority for machine {revoked.machine_id} was revoked at "
                f"{revoked.revoked_at.isoformat()}; this agent cannot rejoin"
            )
            raise AgentAuthorityRevokedError(msg)
        gateway_url = normalize_gateway_url(self.options.gateway_url)
        saved_state = self.state_store.load(gateway_url)
        if (
            saved_state is not None
            and saved_state.capacity_state is not AgentCapacityState.Available
        ):
            return saved_state
        if saved_state is not None and self.options.provider_enrollment_request:
            return saved_state
        if self.options.provider_enrollment_request:
            joined = self._enroll_provider_node(gateway_url=gateway_url)
            self.state_store.save(joined)
            return joined
        join_token = self._join_token()
        if not join_token:
            if saved_state is None:
                msg = "join token is required when no saved agent identity exists"
                raise ValueError(msg)
            return saved_state
        try:
            joined = self._join(join_token, gateway_url=gateway_url)
        except Exception:
            if saved_state is not None:
                return saved_state
            raise
        self.state_store.save(joined)
        self._remove_consumed_installer_token()
        return joined

    def run_stream_iteration(
        self,
        state: AgentState,
        *,
        current_iterations: int = 1,
        tunnel: AgentTunnelService,
        before_agent_update: Callable[[], None],
        tunnel_connected: bool = False,
        runtime_http_url: str = "",
    ) -> AgentDaemonRunResult:
        if state.capacity_notice_at is not None:
            self._capacity_shutdown.arm(state.capacity_notice_at)
        if state.capacity_state is not AgentCapacityState.Available:
            result = self._resume_capacity_interruption(
                state,
                current_iterations=current_iterations,
                tunnel_connected=tunnel_connected,
                runtime_http_url=runtime_http_url,
            )
            if result.capacity_interrupted:
                return result
        timings = StepTimings()
        updater = AgentUpdater.running(self.state_store.state_dir)
        with timings.step("slots"):
            observations = self.worker_controller.observe_workers()
            active_slots = [item.slot for item in observations]
            held_workers = {item.slot.worker_id for item in observations if item.admission_held}
        # The first stream waits briefly for an image check started at boot so it
        # can report the image; later streams never wait on a pull in progress.
        if current_iterations == 1:
            self.worker_controller.wait_for_boot_image(IMAGE_REPORT_WAIT_SECONDS)
        self._reported_worker_images = self.worker_controller.prepared_worker_images()
        with timings.step("stream"):
            stream = self.client.stream_agent(
                StreamAgentRequest(
                    agent_token=state.agent_token,
                    generation=state.release_generation,
                    binary_sha256=updater.binary_sha256(),
                    active_worker_images={
                        slot.worker_id: slot.worker_image
                        for slot in active_slots
                        if slot.worker_image
                    },
                    prepared_worker_images=self._reported_worker_images,
                    admission_waiting_workers=self.worker_controller.waiting_for_admission(
                        active_slots
                    ),
                    booted_since_reserve_prepared=(
                        self.worker_controller.reserve_prepared_in_earlier_boot()
                    ),
                    prepared_stop=read_stop_preparation(self.state_store.state_dir),
                )
            )
        if not stream.ok:
            msg = stream.err_msg or "agent stream rejected"
            if stream.retryable:
                raise AgentStreamRetryableError(msg)
            raise RuntimeError(msg)
        state = _agent_state_from_stream_response(state, stream)
        if stream.resume_from_stop and state.capacity_notice_at is None:
            finish_stop_preparation(self.state_store.state_dir)
            state = state.model_copy(update={"capacity_state": AgentCapacityState.Available})
        self.state_store.save(state)
        if stream.stop_preparation_id:
            self.worker_controller.stop_for_reserve(state.machine_id)
            self._unconfirmed = None
            prepare_machine_storage_for_stop(
                self.state_store.state_dir,
                machine_id=state.machine_id,
                worker_id=agent_machine_worker_id(state.machine_id),
                request_id=stream.stop_preparation_id,
            )
            return AgentDaemonRunResult(
                workspace_id=state.workspace_id,
                placement=state.placement.key,
                machine_id=state.machine_id,
                stream_iterations=current_iterations,
                tunnel_connected=tunnel_connected,
                runtime_http_url=runtime_http_url,
                capacity_state=state.capacity_state,
            )
        if state.capacity_state is not AgentCapacityState.Available:
            return self._resume_capacity_interruption(
                state,
                current_iterations=current_iterations,
                tunnel_connected=tunnel_connected,
                runtime_http_url=runtime_http_url,
            )
        desired_slots = [
            _agent_slot_from_gateway(slot).model_copy(
                update={"agent_binary_sha256": updater.binary_sha256()}
            )
            for slot in stream.slots
        ]
        applied = (
            []
            if stream.reserve is AgentReserveInstruction.Keep and self._holding
            else self._reconcile_workers(
                state,
                desired_slots,
                active_slots,
                held_workers=held_workers,
                preparing=stream.reserve
                in {AgentReserveInstruction.Prepare, AgentReserveInstruction.ResumePending},
                resume_pending=stream.reserve is AgentReserveInstruction.ResumePending,
                tunnel=tunnel,
                timings=timings,
            )
        )
        with timings.step("routes"):
            ready_routes = tunnel.reconcile_routes(
                [
                    AgentTunnelRoute(route.route_id, route.local_target, route.state)
                    for route in stream.routes
                ]
            )
        for route_id in ready_routes:
            self.client.update_agent_route_status(
                UpdateAgentRouteStatusRequest(
                    agent_token=state.agent_token,
                    route_id=route_id,
                    state=BackendRouteState.Ready,
                )
            )
        route_count = len(stream.routes)
        with timings.step("telemetry"):
            telemetry_sent = self._send_telemetry(
                state,
                desired_worker_count=len(desired_slots),
                applied=applied,
            )
        updater.confirm()
        with timings.step("release"):
            release = self.client.agent_release(
                AgentReleaseRequest(
                    agent_token=state.agent_token,
                    generation=state.release_generation,
                    binary_sha256=updater.binary_sha256(),
                )
            )
        actions = (
            ",".join(f"{action.action.value}:{action.worker_id}" for action in applied) or "nothing"
        )
        # A draining machine repeats the same Prepare on every stream; only a
        # change, or a slow iteration, is worth an INFO line.
        changed = actions != self._last_applied_actions
        self._last_applied_actions = actions
        timings.log(
            LOGGER,
            "agent stream %d applied %s at boot+%.2fs",
            current_iterations,
            actions,
            seconds_since_boot(),
            level=(
                logging.INFO
                if changed or timings.total_seconds() >= SLOW_STREAM_ITERATION_SECONDS
                else logging.DEBUG
            ),
        )
        if release.generation < state.release_generation:
            raise RuntimeError("agent release instruction is stale")
        state = state.model_copy(update={"release_generation": release.generation})
        self.state_store.save(state)
        if release.update_agent and release.agent is not None:
            blocker = updater.update_blocker(release.agent)
            if blocker:
                self.client.agent_release(
                    AgentReleaseRequest(
                        agent_token=state.agent_token,
                        generation=release.generation,
                        binary_sha256=updater.binary_sha256(),
                        update_error=blocker,
                    )
                )
                # The machine stays drained for the update, so serving on is no
                # option; exiting for good shows it failed until it is joined again.
                msg = f"cannot update the agent to {release.agent.sha256}: {blocker}"
                raise AgentUpdateBlockedError(msg)
            self.worker_controller.pull_detached(
                {slot.worker_image for slot in desired_slots if slot.worker_image}
            )
            updater.install(release.agent, before_exec=before_agent_update)
        return AgentDaemonRunResult(
            workspace_id=state.workspace_id,
            placement=state.placement.key,
            machine_id=state.machine_id,
            stream_iterations=current_iterations,
            route_count=route_count,
            desired_worker_count=len(desired_slots),
            slot_action_count=len(applied),
            telemetry_sent=telemetry_sent,
            tunnel_connected=tunnel_connected,
            runtime_http_url=runtime_http_url,
        )

    def _reconcile_workers(
        self,
        state: AgentState,
        desired_slots: list[AgentWorkerSlot],
        active_slots: list[AgentWorkerSlot],
        *,
        held_workers: set[str],
        preparing: bool,
        resume_pending: bool,
        tunnel: AgentTunnelService,
        timings: StepTimings,
    ) -> list[AgentWorkerReconcileAction]:
        if self._unconfirmed is not None:
            active_slots = self._settle_unconfirmed_worker(
                self._unconfirmed, desired_slots, active_slots, preparing=preparing
            )
            self._unconfirmed = None
        if preparing:
            active_slots = self._restart_into_hold(
                active_slots, held_workers, machine_id=state.machine_id
            )
        plan = plan_worker_slot_reconciliation(
            desired_slots,
            active_slots,
            executor=self.options.executor,
            os_name=agent_worker_reconcile_os(self.options.os_name),
        )
        network = self.options.worker_network
        bridge = AgentBridgeNetworkConfig(
            bridge_name=network.bridge_name,
            subnet=network.bridge_subnet,
            ipv6_subnet=network.bridge_ipv6_subnet,
        )
        self._holding = preparing
        if preparing:
            tunnel.hold_listeners()
        else:
            tunnel.reconcile_listeners({bridge.gateway} if desired_slots or active_slots else set())
        with timings.step("apply_slots"):
            applied = self.worker_controller.apply(
                plan,
                state.bootstrap,
                active_slots=active_slots,
                reported_images=self._reported_worker_images,
                reserve=preparing,
            )
        running = {slot.worker_id: slot for slot in active_slots}
        for action in applied:
            if action.action is WorkerSlotAction.Stop:
                running.pop(action.worker_id, None)
            elif action.slot is not None and action.action in {
                WorkerSlotAction.Start,
                WorkerSlotAction.Restart,
            }:
                running[action.worker_id] = action.slot
        self._keep_reserve_worker(
            desired_slots,
            running,
            machine_id=state.machine_id,
            preparing=preparing,
            resume_pending=resume_pending,
        )
        return applied

    def _restart_into_hold(
        self, active_slots: list[AgentWorkerSlot], held_workers: set[str], *, machine_id: str
    ) -> list[AgentWorkerSlot]:
        """Stop a reserve's worker that runs without the admission hold.

        One admitted before its preparation began never waits at the fence, so
        a warm preparation could not finish. The plan then starts it again,
        under the hold.
        """
        worker_id = agent_machine_worker_id(machine_id)
        running = next((slot for slot in active_slots if slot.worker_id == worker_id), None)
        if running is None or worker_id in held_workers:
            return active_slots
        LOGGER.info("restarting reserve worker %s under the admission hold", worker_id)
        self.worker_controller.stop_reserve_worker(running)
        return [slot for slot in active_slots if slot.worker_id != worker_id]

    def _keep_reserve_worker(
        self,
        desired_slots: list[AgentWorkerSlot],
        running: dict[str, AgentWorkerSlot],
        *,
        machine_id: str,
        preparing: bool,
        resume_pending: bool,
    ) -> None:
        """Record the worker slot a reserve is prepared with, or clear it once it serves.

        The record is what a boot after the stop starts before its first stream,
        and a worker already running stays unconfirmed until a stream adopts it.
        While a resume is pending the record keeps the boot it was prepared in,
        which is how the control plane tells that resume from a new preparation.
        """
        worker_id = agent_machine_worker_id(machine_id)
        slot = next((item for item in desired_slots if item.worker_id == worker_id), None)
        if not preparing or slot is None:
            self.worker_controller.forget_reserve_worker()
            return
        if not resume_pending:
            self.worker_controller.record_reserve_worker(slot)
        self._unconfirmed = running.get(worker_id)

    def _poll_capacity_interruption(self) -> AgentCapacityInterruptionNotice | None:
        detector = self.interruption_detector
        if detector is None:
            return None
        try:
            return detector()
        except AgentCapacityInterruptionDetectionError as exc:
            self.telemetry.enqueue_event(
                event_type=AgentTelemetryEventType.Agent,
                action="capacity.interruption.poll",
                status="error",
                message="provider interruption sentinel poll failed",
                attrs={"error_type": type(exc).__name__},
            )
            return None

    def _begin_capacity_interruption(
        self,
        state: AgentState,
        notice: AgentCapacityInterruptionNotice,
    ) -> AgentState:
        if notice.notice_at is not None:
            self._capacity_shutdown.arm(notice.notice_at)
        if state.capacity_state in {AgentCapacityState.Preempting, AgentCapacityState.Cordoned}:
            return state
        if (
            state.capacity_state is AgentCapacityState.Draining
            and state.capacity_notice_at is not None
            and notice.notice_at is not None
            and state.capacity_notice_at <= notice.notice_at
        ):
            return state
        self._interruption_reported = False
        updated = state.model_copy(
            update={
                "capacity_state": (
                    AgentCapacityState.Draining
                    if notice.notice_at is not None
                    else AgentCapacityState.Preempting
                ),
                "capacity_reason": notice.reason,
                "capacity_observed_at": _next_capacity_observation(state.capacity_observed_at),
                "capacity_notice_at": notice.notice_at or state.capacity_notice_at,
                "updated_at": utc_now(),
            }
        )
        self.state_store.save(updated)
        return updated

    def _resume_capacity_interruption(
        self,
        state: AgentState,
        *,
        current_iterations: int,
        tunnel_connected: bool,
        runtime_http_url: str,
    ) -> AgentDaemonRunResult:
        if (
            state.capacity_state is AgentCapacityState.Draining
            and not self._capacity_shutdown.due()
        ):
            if state.capacity_notice_at is not None and not self._interruption_reported:
                self._record_capacity_interruption(
                    state,
                    capacity_state=state.capacity_state,
                    reason=state.capacity_reason,
                    observed_at=state.capacity_observed_at or utc_now(),
                    notice_at=state.capacity_notice_at,
                )
                self._interruption_reported = True
            return _capacity_interruption_result(
                state,
                current_iterations=current_iterations,
                tunnel_connected=tunnel_connected,
                runtime_http_url=runtime_http_url,
            )
        if state.capacity_state is AgentCapacityState.Cordoned:
            self._capacity_shutdown.stop()
            return _capacity_interruption_result(
                state,
                current_iterations=current_iterations,
                tunnel_connected=tunnel_connected,
                runtime_http_url=runtime_http_url,
            )
        reason = state.capacity_reason or "agent capacity interruption resumed"
        return self._cordon_and_stop_capacity(
            state,
            reason=reason,
            notice_at=state.capacity_notice_at,
            current_iterations=current_iterations,
            tunnel_connected=tunnel_connected,
            runtime_http_url=runtime_http_url,
        )

    def _cordon_and_stop_capacity(
        self,
        state: AgentState,
        *,
        reason: str,
        notice_at: datetime | None,
        current_iterations: int,
        tunnel_connected: bool,
        runtime_http_url: str,
    ) -> AgentDaemonRunResult:
        cordoned = state.model_copy(update={"capacity_state": AgentCapacityState.Cordoned})
        try:
            cordoned = self._record_capacity_interruption(
                state,
                capacity_state=AgentCapacityState.Cordoned,
                reason=reason,
                observed_at=_next_capacity_observation(state.capacity_observed_at),
                notice_at=notice_at,
            )
        except Exception as exc:
            self.telemetry.enqueue_event(
                event_type=AgentTelemetryEventType.Agent,
                action="capacity.interruption.cordon",
                status="error",
                message="agent capacity cordon confirmation failed",
                attrs={"error_type": type(exc).__name__},
            )
        self._capacity_shutdown.stop()
        return _capacity_interruption_result(
            cordoned,
            current_iterations=current_iterations,
            tunnel_connected=tunnel_connected,
            runtime_http_url=runtime_http_url,
        )

    def _record_capacity_interruption(
        self,
        state: AgentState,
        *,
        capacity_state: AgentCapacityState,
        reason: str,
        observed_at: datetime,
        notice_at: datetime | None,
    ) -> AgentState:
        updated = state.model_copy(
            update={
                "capacity_state": capacity_state,
                "capacity_reason": reason,
                "capacity_observed_at": observed_at,
                "capacity_notice_at": notice_at,
                "updated_at": utc_now(),
            }
        )
        self.state_store.save(updated)
        response = self.client.record_agent_capacity_interruption(
            AgentCapacityInterruptionRequest(
                agent_token=state.agent_token,
                machine_id=state.machine_id,
                credential_id=state.credential_id,
                credential_generation=state.credential_generation,
                state=capacity_state,
                reason=reason,
                observed_at=observed_at,
                notice_at=notice_at,
            )
        )
        if (
            response.machine_id != state.machine_id
            or response.credential_id != state.credential_id
            or response.credential_generation != state.credential_generation
        ):
            raise RuntimeError("gateway returned the wrong agent interruption session")
        updated = state.model_copy(
            update={
                "capacity_state": response.state,
                "capacity_reason": response.reason,
                "capacity_observed_at": response.observed_at,
                "capacity_notice_at": response.notice_at,
                "updated_at": utc_now(),
            }
        )
        self.state_store.save(updated)
        return updated

    def _join(self, join_token: str, *, gateway_url: str) -> AgentState:
        registration = self._machine_registration()
        response = self.client.join_agent(
            JoinAgentRequest(
                join_token=join_token,
                machine_fingerprint=registration.machine_fingerprint,
                hostname=registration.hostname,
                os=self.options.os_name,
                arch=normalize_arch(self.options.arch),
                cpu_count=registration.capacity.cpu_count,
                cpu_millicores=registration.capacity.cpu_millicores,
                memory_mb=registration.capacity.memory_mb,
                gpu=registration.capacity.gpus,
                gpu_ids=registration.capacity.gpu_ids,
                gpu_count=registration.capacity.gpu_count,
                preflight=registration.preflight,
                schedulable=registration.schedulable,
                executor=self.options.executor.value,
            )
        )
        return _agent_state_from_join_response(response, gateway_url=gateway_url)

    def _enroll_provider_node(self, *, gateway_url: str) -> AgentState:
        if self.options.provider is None:
            raise ValueError("provider node enrollment requires a provider")
        self._identity_failure = MachineBootstrapFailureReason.ProviderIdentityFailed
        registration = self._machine_registration()
        proof = self._provider_evidence_provider().create()
        if proof.provider is not self.options.provider:
            raise ValueError("provider node identity evidence returned the wrong provider")
        enrollment = ProviderNodeEnrollmentRequest(
            enrollment_request_id=self.options.provider_enrollment_request,
            provider=proof.provider,
            region=proof.region,
            provider_instance_id=proof.provider_instance_id,
            identity_proof_url=proof.proof_url.get_secret_value(),
            machine_fingerprint=registration.machine_fingerprint,
            hostname=registration.hostname,
            os=self.options.os_name,
            arch=normalize_arch(self.options.arch),
            executor=self.options.executor.value,
            capacity=ProviderNodeCapacity(
                cpu_count=registration.capacity.cpu_count,
                cpu_millicores=registration.capacity.cpu_millicores,
                memory_mb=registration.capacity.memory_mb,
                gpu=registration.capacity.gpus,
                gpu_ids=registration.capacity.gpu_ids,
                gpu_count=registration.capacity.gpu_count,
            ),
            preflight=registration.preflight,
            requested_schedulable=registration.schedulable,
        )
        self._identity_failure = MachineBootstrapFailureReason.AgentEnrollmentFailed
        response = self.client.enroll_provider_node(enrollment)
        return _agent_state_from_join_response(response, gateway_url=gateway_url)

    def _machine_registration(self) -> AgentMachineRegistration:
        resource_detector = self.resource_detector or detect_agent_resource_plan
        detection = resource_detector()
        capacity = resolve_agent_capacity(
            self.options.capacity,
            detection.resources,
            base_checks=detection.checks,
            initially_schedulable=detection.schedulable,
        )
        hostname = self.options.hostname or socket.gethostname()
        return AgentMachineRegistration(
            machine_fingerprint=self.options.machine_fingerprint
            or default_machine_fingerprint(hostname, self.options.os_name, self.options.arch),
            hostname=hostname,
            capacity=capacity.capacity,
            preflight=[
                ComputePreflightCheck(
                    name=check.name.value,
                    ok=check.ok,
                    message=check.message,
                    severity=check.severity,
                )
                for check in capacity.checks
            ],
            schedulable=capacity.schedulable,
        )

    def _join_token(self) -> str:
        token = self.options.join_token.strip()
        if token:
            return token
        if not self.options.join_token_file:
            return ""
        try:
            return Path(self.options.join_token_file).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def _remove_consumed_installer_token(self) -> None:
        if not self.options.join_token_file:
            return
        token_path = Path(self.options.join_token_file).expanduser().absolute()
        installer_token_path = (self.state_store.state_dir / "join-token").expanduser().absolute()
        if token_path == installer_token_path:
            token_path.unlink(missing_ok=True)

    def _send_telemetry(
        self,
        state: AgentState,
        *,
        desired_worker_count: int,
        applied: list[AgentWorkerReconcileAction],
    ) -> bool:
        self.telemetry.set_metrics(
            agent_metric_snapshot(self.state_store.state_dir, desired_worker_count)
        )
        for action in applied:
            self.telemetry.enqueue_event(
                event_type=AgentTelemetryEventType.Agent,
                action=f"worker.{action.action.value}",
                status=action.action.value,
                message=action.reason,
                attrs={"worker_id": action.worker_id},
            )
        return self.telemetry.flush(state.agent_token, self.client) > 0

    def _join_step[T](self, action: str, operation: Callable[[], T]) -> T:
        """Run a startup step, retrying failures that never reached the control plane.

        The stream loop already tolerates transient failures, but everything before
        it ran once and exited. A single gateway TLS reset therefore killed the agent
        process, and systemd eventually stopped restarting it, leaving a billing
        machine with no agent. Startup gets the same tolerance as steady state.

        The unit starts beside the network rather than after it, so a booting
        machine's first attempts fail until DHCP finishes, about a second later.
        Retries begin short for that reason and double toward 30 seconds so an
        outage costs the gateway a few calls per agent. The budget counts only
        the backoff, so a gateway that hangs each call to its timeout still gets
        as many attempts as one that refuses them.
        """
        backed_off = 0.0
        attempt = 0
        while True:
            try:
                return operation()
            except Exception as exc:
                attempt += 1
                remaining = JOIN_RETRY_SECONDS - backed_off
                if remaining <= 0 or not _recoverable_stream_error(exc):
                    raise
                delay = min(
                    JOIN_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                    JOIN_RETRY_MAX_SECONDS,
                    remaining,
                )
                self.telemetry.enqueue_event(
                    event_type=AgentTelemetryEventType.Agent,
                    action=f"{action}.retry",
                    status="retrying",
                    message="agent startup step failed; retrying",
                    attrs={
                        "error_type": type(exc).__name__,
                        "attempt": str(attempt),
                    },
                )
                time.sleep(delay)
                backed_off += delay

    def _start_tunnel(self, state: AgentState, *, hold_worker_control: bool) -> AgentTunnelService:
        network = self.options.worker_network
        bridge = AgentBridgeNetworkConfig(
            bridge_name=network.bridge_name,
            subnet=network.bridge_subnet,
            ipv6_subnet=network.bridge_ipv6_subnet,
        )
        registered = self.client.list_agent_routes(
            ListAgentRoutesRequest(agent_token=state.agent_token)
        )
        tunnel = AgentTunnelService(
            state_dir=self.state_store.state_dir,
            identity=AgentTunnelIdentity(
                workspace_id=state.workspace_id,
                enrollment_id=state.credential_id,
                credential_generation=state.credential_generation,
            ),
            issue_certificate=self.client.issue_agent_certificate,
            agent_token=state.agent_token,
            callback_firewall=AgentBridgeCallbackFirewall(
                config=bridge,
                owner_id=state.machine_id,
            ),
        )
        tunnel.start(
            callback_hosts={bridge.gateway},
            routes=[
                AgentTunnelRoute(route.route_id, route.local_target, route.state)
                for route in registered.routes
            ],
            listen=not hold_worker_control,
        )
        return tunnel


def build_agent_daemon_service(
    options: AgentDaemonOptions,
    *,
    client: AgentGatewayClient | None = None,
    worker_controller: DockerAgentWorkerController | None = None,
    resource_detector: AgentResourceDetector | None = None,
    interruption_detector: AgentCapacityInterruptionDetector | None = None,
    provider_identity: ProviderNodeIdentityProofProvider | None = None,
) -> AgentDaemonService:
    state_dir = Path(options.state_dir)
    telemetry = AgentTelemetryBuffer()

    def report_worker_exit(worker_id: str, line: str) -> None:
        telemetry.enqueue_log(
            line,
            source=AgentTelemetrySource.Worker,
            stream=AgentTelemetryStream.Stderr,
            worker_id=worker_id,
            level="error",
        )

    return AgentDaemonService(
        options=options,
        client=client or HttpAgentGatewayClient.from_options(options),
        state_store=AgentStateStore(state_dir),
        telemetry=telemetry,
        worker_controller=worker_controller
        or DockerAgentWorkerController(
            state_dir,
            docker_binary=options.docker_binary,
            worker_image_override=options.worker_image,
            worker_network=options.worker_network,
            host_aliases=list(dict.fromkeys(options.worker_host_aliases)),
            platform=agent_worker_platform(options.os_name, options.arch),
            report_worker_exit=report_worker_exit,
            disk_volume_slots=_provider_disk_volume_slots(options),
            docker_wait_seconds=DOCKER_WAIT_SECONDS,
        ),
        resource_detector=resource_detector,
        interruption_detector=(
            interruption_detector or _provider_capacity_interruption_detector(options)
        ),
        provider_identity=provider_identity,
    )


def _provider_disk_volume_slots(options: AgentDaemonOptions) -> Callable[[], int] | None:
    """Reads the machine's free volume attachments; None on a joined machine."""
    if options.provider is None:
        return None
    if options.provider is not ProviderKind.Aws:
        raise ValueError(f"provider {options.provider.value!r} has no disk volume support")
    # Here, not at the top: the instance catalog it reads costs a machine a
    # customer joined tens of milliseconds at every start, for nothing.
    from provider_aws.volume_attachments import AwsInstanceDiskVolumeSlots

    return AwsInstanceDiskVolumeSlots().slots


def _provider_capacity_interruption_detector(
    options: AgentDaemonOptions,
) -> AgentCapacityInterruptionDetector | None:
    if options.provider is None:
        return None
    monitor = AwsEc2SpotInterruptionMonitor()

    def detect() -> AgentCapacityInterruptionNotice | None:
        try:
            notice = monitor.poll()
        except AwsSpotInterruptionMonitorError as exc:
            raise AgentCapacityInterruptionDetectionError(str(exc)) from exc
        if notice is None:
            return None
        return AgentCapacityInterruptionNotice(
            reason=f"aws-ec2-spot-{notice.action.value}",
            observed_at=notice.observed_at,
            notice_at=notice.notice_at,
        )

    return detect


def detect_agent_resource_plan(
    *, probes: AgentPreflightProbeSet | None = None
) -> AgentResourceDetection:
    cpu_count = max(os.cpu_count() or 1, 1)
    gpus = detect_nvidia_gpu_devices()
    preflight = plan_agent_preflight(probes or _detect_agent_preflight_probes(gpus))
    checks = [
        AgentCapacityCheck(
            name=PreflightCheckName(check.name),
            ok=check.ok,
            message=check.message,
            severity=check.severity,
        )
        for check in preflight.checks
    ]
    return AgentResourceDetection(
        resources=AgentDetectedResources(
            cpu_count=cpu_count,
            memory_mb=physical_memory_mb(),
            gpus=gpus,
        ),
        checks=checks,
        schedulable=preflight.schedulable,
    )


def _detect_agent_preflight_probes(gpus: list[AgentGpuDevice]) -> AgentPreflightProbeSet:
    docker = shutil.which("docker")
    iptables = shutil.which("iptables")
    ip_forward = Path("/proc/sys/net/ipv4/ip_forward")
    return AgentPreflightProbeSet(
        os_name=platform.system().lower(),
        worker_container_executor=True,
        agent_in_container=Path("/.dockerenv").exists(),
        effective_uid=os.geteuid(),
        docker_command=docker is not None,
        docker_socket=Path("/var/run/docker.sock").exists(),
        docker_daemon=docker is not None
        and _command_succeeds([docker, "info", "--format", "{{.ServerVersion}}"]),
        docker_host_network=docker is not None
        and _command_succeeds([docker, "network", "inspect", "host"]),
        network_namespace=shutil.which("ip") is not None and Path("/proc/self/ns/net").exists(),
        netns_run_dir_writable=_directory_or_parent_writable(Path("/var/run/netns")),
        ip_forward_enabled_or_writable=_ip_forward_enabled_or_writable(ip_forward),
        iptables_nat=iptables is not None and _command_succeeds([iptables, "-t", "nat", "-S"]),
        network_manager=shutil.which("nmcli") is not None,
        fuse_device=Path("/dev/fuse").exists(),
        nvidia_gpu_names=[gpu.name for gpu in gpus],
        nvidia_runtime=nvidia_container_runtime_available(),
    )


def _command_succeeds(command: list[str]) -> bool:
    try:
        return (
            subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _directory_or_parent_writable(path: Path) -> bool:
    target = path if path.exists() else path.parent
    return target.is_dir() and os.access(target, os.W_OK)


def _ip_forward_enabled_or_writable(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8").strip() == "1" or os.access(path, os.W_OK)
    except OSError:
        return False


def detect_nvidia_gpu_devices() -> list[AgentGpuDevice]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name",
                "--format=csv,noheader",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return []
    return parse_nvidia_smi_gpu_devices(result.stdout)


def nvidia_container_runtime_available() -> bool:
    return (
        shutil.which("nvidia-ctk") is not None
        or shutil.which("nvidia-container-runtime") is not None
    )


def default_machine_fingerprint(
    hostname: str,
    os_name: str,
    arch: str,
    *,
    machine_id_paths: tuple[Path, ...] = DEFAULT_MACHINE_ID_PATHS,
) -> str:
    machine_ids: list[str] = []
    for path in machine_id_paths:
        try:
            machine_ids.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return machine_fingerprint(
        hostname,
        os_name=os_name,
        arch=normalize_arch(arch),
        machine_ids=machine_ids,
    )


def normalize_arch(value: str) -> str:
    lowered = value.lower()
    if lowered in {"x86_64", "amd64"}:
        return "amd64"
    if lowered in {"aarch64", "arm64"}:
        return "arm64"
    return lowered


def agent_worker_platform(os_name: str, arch: str) -> str:
    normalized_os = os_name.lower()
    if normalized_os == "darwin":
        normalized_os = "linux"
    return f"{normalized_os}/{normalize_arch(arch)}"


def agent_worker_reconcile_os(os_name: str) -> str:
    return "linux" if os_name.lower() == "darwin" else os_name


def _agent_slot_from_gateway(slot: http.AgentWorkerSlot) -> AgentWorkerSlot:
    return AgentWorkerSlot(
        worker_id=slot.worker_id,
        worker_token=slot.worker_token,
        placement=slot.placement,
        capacity_owner_id=slot.capacity_owner_id,
        billing_owner=slot.billing_owner,
        machine_id=slot.machine_id,
        cpu_millicores=slot.cpu,
        memory_mb=slot.memory,
        gpu=slot.gpu,
        gpu_count=slot.gpu_count,
        gpu_assignment=slot.gpu_assignment,
        network_prefix=slot.network_prefix,
        worker_image=slot.worker_image,
        status=slot.status,
    )


def _agent_bootstrap(
    config: AgentBootstrapConfig | None,
    *,
    fallback_gateway_url: str,
) -> AgentBootstrap:
    if config is None:
        return AgentBootstrap(
            gateway_public_http_url=fallback_gateway_url,
        )
    return AgentBootstrap(
        gateway_public_http_url=config.gateway_public_http_url or fallback_gateway_url,
        gateway_grpc_host=config.gateway_grpc_host,
        gateway_grpc_port=config.gateway_grpc_port,
        gateway_grpc_tls=config.gateway_grpc_tls,
        image_local_cache_enabled=config.image_local_cache_enabled,
        image_registry_store=config.image_registry_store,
        image_clip_version=config.image_clip_version,
    )


def _agent_state_from_join_response(
    response: JoinAgentResponse,
    *,
    gateway_url: str,
) -> AgentState:
    if not response.agent_token:
        raise RuntimeError("gateway did not return an agent token")
    if not response.credential_id:
        raise RuntimeError("gateway did not return the agent credential session")
    return AgentState(
        gateway_url=gateway_url,
        workspace_id=response.workspace_id,
        placement=response.placement,
        machine_id=response.machine_id,
        agent_token=response.agent_token,
        credential_id=response.credential_id,
        credential_generation=response.credential_generation,
        capacity_state=response.capacity_state,
        bootstrap=_agent_bootstrap(response.bootstrap, fallback_gateway_url=gateway_url),
    )


def _agent_state_from_stream_response(
    state: AgentState,
    response: StreamAgentResponse,
) -> AgentState:
    if response.generation < state.release_generation:
        raise AgentStreamRetryableError("agent worker instruction is stale")
    if (
        not response.credential_id
        or response.credential_id != state.credential_id
        or response.credential_generation != state.credential_generation
    ):
        raise RuntimeError("gateway returned the wrong agent stream session")
    return state.model_copy(
        update={
            "release_generation": response.generation,
            "capacity_state": (
                state.capacity_state
                if state.capacity_state is not AgentCapacityState.Available
                and response.capacity_state is AgentCapacityState.Available
                else response.capacity_state
            ),
            "bootstrap": (
                state.bootstrap
                if response.bootstrap is None
                else _agent_bootstrap(
                    response.bootstrap,
                    fallback_gateway_url=state.sanitized_gateway_url,
                )
            ),
            "updated_at": utc_now(),
        }
    )


def _next_capacity_observation(previous: datetime | None) -> datetime:
    current = utc_now()
    if previous is None or current > previous:
        return current
    return previous + timedelta(microseconds=1)


def _capacity_interruption_result(
    state: AgentState,
    *,
    current_iterations: int,
    tunnel_connected: bool,
    runtime_http_url: str,
) -> AgentDaemonRunResult:
    return AgentDaemonRunResult(
        workspace_id=state.workspace_id,
        placement=state.placement.key,
        machine_id=state.machine_id,
        stream_iterations=current_iterations,
        tunnel_connected=tunnel_connected,
        runtime_http_url=runtime_http_url,
        capacity_state=state.capacity_state,
        capacity_interrupted=state.capacity_state is not AgentCapacityState.Draining,
    )


def _agent_lock_is_stale(path: Path, *, current_pid: int) -> bool:
    try:
        lock_pid = _agent_lock_pid(path.read_text(encoding="utf-8"))
    except OSError:
        return False
    if lock_pid <= 0:
        return False
    if lock_pid == current_pid:
        return True
    try:
        os.kill(lock_pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _agent_lock_pid(contents: str) -> int:
    for line in contents.splitlines():
        key, separator, value = line.partition("=")
        if key == "pid" and separator:
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


def _provider_node_identity() -> ProviderNodeIdentityProofProvider:
    # Imported here so a machine a customer joined never loads botocore or the
    # AWS provider at startup; only a provider node asks for a proof.
    from provider_aws.provider_node_proof import AwsProviderNodeIdentityProofProvider

    return AwsProviderNodeIdentityProofProvider()


def _recoverable_stream_error(exc: Exception) -> bool:
    if isinstance(exc, AgentAuthorityRevokedError | AgentTunnelRevokedError):
        return False
    if isinstance(exc, HttpApiError):
        return exc.status_code >= 500
    # A transport error means the request never reached a response, so the
    # control plane has not rejected anything and retrying is always correct.
    # These arrive as HttpTransportError, which is a plain RuntimeError, so it
    # has to be named explicitly or every TLS reset reads as a fatal error.
    if isinstance(exc, HttpTransportError):
        return True
    if isinstance(
        exc,
        AgentStreamRetryableError | WorkerImagePullError | ProviderNodeIdentityUnavailableError,
    ):
        return True
    return isinstance(
        exc,
        ConnectionError
        | TimeoutError
        | OSError
        | http_client.HTTPException
        | urllib.error.URLError,
    )


def agent_authority_was_revoked(exc: Exception) -> bool:
    if isinstance(exc, AgentAuthorityRevokedError | AgentTunnelRevokedError):
        return True
    if not isinstance(exc, HttpApiError) or not 400 <= exc.status_code < 500:
        return False
    detail = (exc.detail or str(exc)).strip().lower()
    return detail in AGENT_AUTHORITY_REVOKED_DETAILS
