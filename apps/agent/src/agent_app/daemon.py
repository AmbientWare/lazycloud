from __future__ import annotations

import hmac
import http.client as http_client
import json
import logging
import math
import os
import platform
import shutil
import socket
import subprocess
import time
import traceback
import urllib.error
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Protocol

from agent.operations import (
    AGENT_AUTHORITY_REVOKED_FILE,
    AGENT_RUNTIME_READY_FILE,
    AgentAuthorityRevoked,
    AgentBootstrap,
    AgentCapacity,
    AgentCapacityCheck,
    AgentCapacityInterruptionNotice,
    AgentCapacityOptions,
    AgentDetectedResources,
    AgentGpuDevice,
    AgentResourceDetection,
    AgentRuntimeReady,
    AgentState,
    AgentWorkerNetwork,
    AgentWorkerReconcileAction,
    AgentWorkerReconcilePlan,
    AgentWorkerSlot,
    WorkerExecutor,
    WorkerSlotAction,
    agent_state_matches_gateway,
    agent_state_payload,
    normalize_gateway_url,
    parse_nvidia_smi_gpu_devices,
    plan_agent_lock,
    plan_worker_container,
    plan_worker_slot_reconciliation,
    resolve_agent_capacity,
    sanitize_worker_name,
)
from agent.service_manager import (
    DEFAULT_AGENT_STATE_DIR,
    AgentPreflightProbeSet,
    PreflightCheckName,
    machine_fingerprint,
    plan_agent_preflight,
)
from compute.projection import normalize_backend_route_transport
from gateway.http import (
    AgentBootstrapConfig,
    AgentTelemetryRequest,
    AgentTelemetryResponse,
    JoinAgentRequest,
    JoinAgentResponse,
    LeaveAgentRequest,
    LeaveAgentResponse,
    RegisterAgentTailnetDeviceRequest,
    RegisterAgentTailnetDeviceResponse,
    RequestAgentTransportCredentialRequest,
    RequestAgentTransportCredentialResponse,
    StreamAgentRequest,
    StreamAgentResponse,
    UpdateAgentRouteStatusRequest,
    UpdateAgentRouteStatusResponse,
)
from networking.dialer import TailnetPeerRuntime
from networking.tailnet import (
    TailnetAuthenticationRequired,
    TailnetRuntime,
    TailnetRuntimeMode,
    TailnetRuntimeOptions,
    TailnetStatus,
)
from provider_aws import (
    AwsEc2SpotInterruptionMonitor,
    AwsSpotInterruptionMonitorError,
)
from provider_clients import (
    ProviderNodeIdentityEvidence,
    ProviderNodeIdentityEvidenceProvider,
    provider_node_identity_evidence_provider,
)
from pydantic import Field, JsonValue, TypeAdapter, field_validator, model_validator
from shared.app_identity import AGENT_NAME, AGENT_TAILNET_DIR_NAME
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputePreflightCheck,
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
)
from shared.contracts import ContractModel
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
from shared.http_transport import HttpChannel
from shared.provider_config import ProviderKind
from shared.routing import BackendRouteState, BackendRouteTransport
from shared.timestamps import utc_now
from worker.configuration import WorkerConfiguration, serialize_worker_configuration

from agent_app.metrics import agent_metric_snapshot, physical_memory_mb
from agent_app.route_proxy import (
    AgentRouteProxyConfig,
    AgentRouteProxyService,
    local_target_ready,
)
from agent_app.telemetry import AgentTelemetryBuffer, AgentTelemetryEventType
from gateway import http

LOGGER = logging.getLogger(__name__)

DEFAULT_AGENT_STREAM_INTERVAL_SECONDS = 5.0
DEFAULT_AGENT_HTTP_TIMEOUT_SECONDS = 30.0
DEFAULT_AGENT_INTERRUPTION_GRACE_SECONDS = 90.0
NVIDIA_SMI_TIMEOUT_SECONDS = 5.0
JOIN_MAX_ATTEMPTS = 6
JOIN_RETRY_BASE_SECONDS = 2.0
JOIN_RETRY_MAX_SECONDS = 30.0
AGENT_STATE_FILE = "agent-state.json"
AGENT_ACTIVE_SLOTS_FILE = "active-worker-slots.json"
DEFAULT_MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
# The control plane rejects a device registration with this when its enrollment
# carries no issued identity yet. It means the agent's local session and the
# control plane have diverged — recoverable by re-enrolling, unlike a revoked
# authority — so it is named rather than matched inline.
TAILNET_ENROLLMENT_NOT_AWAITING_DETAIL = "tailnet enrollment is not awaiting this device"
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
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

type AgentResourceDetector = Callable[[], AgentResourceDetection]
type AgentCapacityInterruptionDetector = Callable[[], AgentCapacityInterruptionNotice | None]


class ProviderInstanceIdentityMode(StrEnum):
    ImdsV2 = "imds-v2"


class AgentCapacityInterruptionDetectionError(RuntimeError):
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
    worker_route_target: str = "127.0.0.1"
    worker_runtime_http_url: str = ""
    worker_network: AgentWorkerNetwork = Field(default_factory=AgentWorkerNetwork)
    worker_host_aliases: list[str] = Field(default_factory=list)
    docker_binary: str = "docker"
    stream_interval_seconds: float = DEFAULT_AGENT_STREAM_INTERVAL_SECONDS
    http_timeout_seconds: float = DEFAULT_AGENT_HTTP_TIMEOUT_SECONDS
    interruption_grace_seconds: float = DEFAULT_AGENT_INTERRUPTION_GRACE_SECONDS
    once: bool = False
    capacity: AgentCapacityOptions = Field(default_factory=AgentCapacityOptions)
    route_proxy: AgentRouteProxyConfig = Field(default_factory=AgentRouteProxyConfig)
    tailnet_mode: TailnetRuntimeMode = TailnetRuntimeMode.Managed
    tailnet_state_dir: str = ""
    tailnet_socket_path: str = ""
    tailnet_tailscale_binary: str = "tailscale"
    tailnet_tailscaled_binary: str = "tailscaled"
    tailnet_userspace_networking: bool = False

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
        if all(provider_values) and self.provider is not ProviderKind.Aws:
            raise ValueError("provider enrollment is only supported for AWS")
        return self


class AgentDaemonRunResult(ContractModel):
    workspace_id: str = ""
    pool_name: str = ""
    machine_id: str = ""
    stream_iterations: int = 0
    route_count: int = 0
    route_proxy_target: str = ""
    desired_worker_count: int = 0
    slot_action_count: int = 0
    telemetry_sent: bool = False
    tailnet_started: bool = False
    tailnet_hostname: str = ""
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

    def record_agent_capacity_interruption(
        self,
        request: AgentCapacityInterruptionRequest,
    ) -> AgentCapacityInterruptionResponse: ...

    def update_agent_route_status(
        self,
        request: UpdateAgentRouteStatusRequest,
    ) -> UpdateAgentRouteStatusResponse: ...

    def request_agent_transport_credential(
        self,
        request: RequestAgentTransportCredentialRequest,
    ) -> RequestAgentTransportCredentialResponse: ...

    def register_agent_tailnet_device(
        self,
        request: RegisterAgentTailnetDeviceRequest,
    ) -> RegisterAgentTailnetDeviceResponse: ...

    def stream_agent_telemetry(
        self,
        request: AgentTelemetryRequest,
    ) -> AgentTelemetryResponse: ...


class AgentTailnetRuntime(TailnetPeerRuntime, Protocol):
    """The agent's view of its tailnet runtime.

    Extends the shared peer runtime rather than narrowing it. The agent is the
    only process on a node holding a tailnet client, so declaring less than the
    runtime implements left peer resolution unreachable — including to the
    worker, which has no client of its own and must not be handed the tailscaled
    socket, since that grants tailnet control rather than lookup.
    """

    def authenticate(
        self,
        *,
        auth_key: str,
        hostname: str,
        control_url: str = "",
        force: bool = False,
    ) -> TailnetStatus: ...

    def status(self) -> TailnetStatus: ...


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
            self.channel.post("/gateway/agents/join", _payload(request))
        )

    def enroll_provider_node(
        self,
        request: ProviderNodeEnrollmentRequest,
    ) -> JoinAgentResponse:
        return JoinAgentResponse.model_validate(
            self.channel.post("/gateway/provider-nodes/enroll", _payload(request))
        )

    def record_provider_node_bootstrap_failure(
        self,
        request: ProviderNodeBootstrapFailureRequest,
    ) -> ProviderNodeBootstrapFailureResponse:
        return ProviderNodeBootstrapFailureResponse.model_validate(
            self.channel.post("/gateway/provider-nodes/bootstrap-failure", _payload(request))
        )

    def record_provider_node_bootstrap_phase(
        self,
        request: ProviderNodeBootstrapPhaseRequest,
    ) -> ProviderNodeBootstrapFailureResponse:
        return ProviderNodeBootstrapFailureResponse.model_validate(
            self.channel.post("/gateway/provider-nodes/bootstrap-phase", _payload(request))
        )

    def leave_agent(self, request: LeaveAgentRequest) -> LeaveAgentResponse:
        return LeaveAgentResponse.model_validate(
            self.channel.post("/gateway/agents/leave", _payload(request))
        )

    def stream_agent(self, request: StreamAgentRequest) -> StreamAgentResponse:
        return StreamAgentResponse.model_validate(
            self.channel.post("/gateway/agents/stream", _payload(request))
        )

    def record_agent_capacity_interruption(
        self,
        request: AgentCapacityInterruptionRequest,
    ) -> AgentCapacityInterruptionResponse:
        return AgentCapacityInterruptionResponse.model_validate(
            self.channel.post("/gateway/agents/capacity-interruption", _payload(request))
        )

    def update_agent_route_status(
        self,
        request: UpdateAgentRouteStatusRequest,
    ) -> UpdateAgentRouteStatusResponse:
        return UpdateAgentRouteStatusResponse.model_validate(
            self.channel.post("/gateway/agents/routes/status", _payload(request))
        )

    def request_agent_transport_credential(
        self,
        request: RequestAgentTransportCredentialRequest,
    ) -> RequestAgentTransportCredentialResponse:
        return RequestAgentTransportCredentialResponse.model_validate(
            self.channel.post("/gateway/agents/transport-credential", _payload(request))
        )

    def register_agent_tailnet_device(
        self,
        request: RegisterAgentTailnetDeviceRequest,
    ) -> RegisterAgentTailnetDeviceResponse:
        return RegisterAgentTailnetDeviceResponse.model_validate(
            self.channel.post("/gateway/agents/tailnet-device", _payload(request))
        )

    def stream_agent_telemetry(
        self,
        request: AgentTelemetryRequest,
    ) -> AgentTelemetryResponse:
        return AgentTelemetryResponse.model_validate(
            self.channel.post("/gateway/agents/telemetry", _payload(request))
        )


@dataclass(slots=True)
class AgentStateStore:
    state_dir: Path
    filename: str = AGENT_STATE_FILE

    @property
    def path(self) -> Path:
        return self.state_dir / self.filename

    @property
    def ready_path(self) -> Path:
        return self.state_dir / AGENT_RUNTIME_READY_FILE

    def load(self, gateway_url: str) -> AgentState | None:
        if not self.path.exists():
            return None
        state = AgentState.model_validate_json(self.path.read_text(encoding="utf-8"))
        if agent_state_matches_gateway(state, gateway_url):
            return state
        return None

    def save(self, state: AgentState) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        payload = _JSON_VALUE_ADAPTER.validate_python(agent_state_payload(state))
        _write_json_atomic(self.path, payload, permissions=0o600)

    @property
    def revoked_path(self) -> Path:
        return self.state_dir / AGENT_AUTHORITY_REVOKED_FILE

    def begin_run(self) -> None:
        self.ready_path.unlink(missing_ok=True)

    def mark_authority_revoked(self, state: AgentState) -> None:
        """Record that this machine's authority is gone, and drop its identity.

        Revocation is terminal, unlike every other reason the stream ends. The
        saved identity is what a restart would re-present, so it goes with it —
        leaving it behind is what let a revoked agent rejoin and be rejected on
        a loop, on a machine that keeps billing.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        marker = AgentAuthorityRevoked(machine_id=state.machine_id)
        _write_json_atomic(self.revoked_path, _payload(marker), permissions=0o600)
        self.path.unlink(missing_ok=True)

    def authority_revoked(self) -> AgentAuthorityRevoked | None:
        if not self.revoked_path.exists():
            return None
        return AgentAuthorityRevoked.model_validate_json(
            self.revoked_path.read_text(encoding="utf-8")
        )

    def mark_ready(self, state: AgentState, *, stream_iteration: int) -> None:
        marker = AgentRuntimeReady(
            machine_id=state.machine_id,
            stream_iteration=stream_iteration,
        )
        _write_json_atomic(
            self.ready_path,
            _payload(marker),
            permissions=0o600,
        )


@dataclass(slots=True)
class AgentProcessLock:
    path: Path
    fd: int

    @classmethod
    def acquire(cls, state_dir: Path, *, pid: int | None = None) -> AgentProcessLock:
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        plan = plan_agent_lock(str(state_dir), pid=pid or os.getpid())
        lock_path = Path(plan.path)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, plan.permissions)
        except FileExistsError as exc:
            if _agent_lock_is_stale(lock_path, current_pid=pid or os.getpid()):
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


class CommandRunner(Protocol):
    def run(self, args: list[str]) -> CommandResult: ...


class CommandResult(ContractModel):
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class SubprocessCommandRunner:
    def run(self, args: list[str]) -> CommandResult:
        result = subprocess.run(args, text=True, capture_output=True, check=False)
        return CommandResult(
            args=args,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def _slot_removal_is_settled(detail: str) -> bool:
    """Report whether a failed ``docker rm -f`` already achieves the desired state.

    A concurrent removal ("removal ... is already in progress") and an absent
    container both mean the slot is going away, which is exactly what stopping
    asks for. Treating them as errors makes reconciliation retry forever, so the
    slot is never recreated and its worker never leaves ``pending``.
    """
    message = detail.casefold()
    return "already in progress" in message or "no such container" in message


@dataclass(slots=True)
class DockerAgentWorkerController:
    state_dir: Path
    docker_binary: str = "docker"
    worker_image_override: str = ""
    worker_runtime_http_url_override: str = ""
    target_host: str = "127.0.0.1"
    worker_network: AgentWorkerNetwork = field(default_factory=AgentWorkerNetwork)
    runner: CommandRunner = field(default_factory=SubprocessCommandRunner)
    host_aliases: list[str] = field(default_factory=list)
    platform: str = ""
    peer_resolver_address: str = ""
    tailnet_dns_suffix: str = ""

    @property
    def active_slots_path(self) -> Path:
        return self.state_dir / AGENT_ACTIVE_SLOTS_FILE

    def active_slots(self) -> list[AgentWorkerSlot]:
        if not self.active_slots_path.exists():
            return []
        raw = _JSON_VALUE_ADAPTER.validate_json(self.active_slots_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        slots = [AgentWorkerSlot.model_validate(item) for item in raw]
        return [slot for slot in slots if self._slot_container_running(slot)]

    def apply(
        self,
        plan: AgentWorkerReconcilePlan,
        bootstrap: AgentBootstrap,
        *,
        worker_repository_url: str,
    ) -> list[AgentWorkerReconcileAction]:
        active_by_id = {slot.worker_id: slot for slot in self.active_slots()}
        applied: list[AgentWorkerReconcileAction] = []
        for action in plan.actions:
            if action.action in {WorkerSlotAction.Stop, WorkerSlotAction.Restart}:
                slot = active_by_id.pop(action.worker_id, None) or action.slot
                if slot is not None:
                    self._stop(slot)
                    applied.append(action)
            if action.action in {WorkerSlotAction.Start, WorkerSlotAction.Restart}:
                if action.slot is None:
                    continue
                self._start(
                    action.slot,
                    bootstrap,
                    worker_repository_url=worker_repository_url,
                )
                active_by_id[action.worker_id] = action.slot
                applied.append(action)
        self._save_active_slots(list(active_by_id.values()))
        return applied

    def stop_all(self) -> None:
        for slot in self.active_slots():
            self._stop(slot)
        self._save_active_slots([])

    def gracefully_stop_all(self, *, grace_seconds: float) -> None:
        if grace_seconds <= 0:
            raise ValueError("worker shutdown grace must be positive")
        slots = self.active_slots()
        if not slots:
            self._save_active_slots([])
            return
        names = [f"{AGENT_NAME}-{sanitize_worker_name(slot.worker_id)}" for slot in slots]
        stop_result = self.runner.run(
            [
                self.docker_binary,
                "stop",
                "--timeout",
                str(math.ceil(grace_seconds)),
                *names,
            ]
        )
        remove_result = self.runner.run([self.docker_binary, "rm", "-f", *names])
        self._save_active_slots([])
        if stop_result.returncode != 0:
            msg = f"graceful worker shutdown failed: {stop_result.stderr or stop_result.stdout}"
            raise RuntimeError(msg)
        if remove_result.returncode != 0:
            msg = f"remove stopped workers failed: {remove_result.stderr or remove_result.stdout}"
            raise RuntimeError(msg)

    def _start(
        self,
        slot: AgentWorkerSlot,
        bootstrap: AgentBootstrap,
        *,
        worker_repository_url: str,
    ) -> None:
        image = self.worker_image_override or slot.worker_image
        if not image:
            msg = f"worker image is required for slot {slot.worker_id}"
            raise ValueError(msg)
        worker_bootstrap = (
            bootstrap.model_copy(
                update={"gateway_runtime_http_url": self.worker_runtime_http_url_override}
            )
            if self.worker_runtime_http_url_override
            else bootstrap
        )
        plan = plan_worker_container(
            worker_bootstrap,
            slot,
            worker_repository_url=worker_repository_url,
            state_dir=str(self.state_dir),
            image=image,
            target_host=self.target_host,
            platform=self.platform,
            host_aliases=self.host_aliases,
            network=self.worker_network,
            peer_resolver_address=self.peer_resolver_address,
            tailnet_dns_suffix=self.tailnet_dns_suffix,
        )
        for path in plan.dirs.all_paths():
            Path(path).mkdir(parents=True, exist_ok=True)
        _write_worker_configuration_atomic(
            Path(plan.config_path),
            plan.config,
            permissions=0o600,
        )
        args = [self.docker_binary, plan.docker_args[0], "--detach", *plan.docker_args[1:]]
        result = self.runner.run(args)
        if result.returncode != 0:
            msg = f"start worker slot {slot.worker_id} failed: {result.stderr or result.stdout}"
            raise RuntimeError(msg)

    def _stop(self, slot: AgentWorkerSlot) -> None:
        name = f"{AGENT_NAME}-{sanitize_worker_name(slot.worker_id)}"
        result = self.runner.run([self.docker_binary, "rm", "-f", name])
        if result.returncode != 0 and not _slot_removal_is_settled(result.stderr or result.stdout):
            msg = f"stop worker slot {slot.worker_id} failed: {result.stderr or result.stdout}"
            raise RuntimeError(msg)

    def _slot_container_running(self, slot: AgentWorkerSlot) -> bool:
        name = f"{AGENT_NAME}-{sanitize_worker_name(slot.worker_id)}"
        result = self.runner.run([self.docker_binary, "inspect", "-f", "{{.State.Running}}", name])
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    def _save_active_slots(self, slots: list[AgentWorkerSlot]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload: list[JsonValue] = [
            _payload(slot) for slot in sorted(slots, key=lambda item: item.worker_id)
        ]
        _write_json_atomic(self.active_slots_path, payload, permissions=0o600)


@dataclass(slots=True)
class WorkerTailnetPeerResolver:
    """Resolves tailnet peers for the workers this agent launched.

    A worker holds no tailnet client and is deliberately not given the
    tailscaled socket, which grants control rather than lookup. It presents the
    token this agent issued it, so the check is against slots the agent already
    tracks and no additional secret exists to distribute or rotate.
    """

    runtime: AgentTailnetRuntime
    worker_controller: DockerAgentWorkerController
    wait_seconds: float = 10.0

    def resolve_for_worker(self, host: str, worker_token: str) -> str:
        if not worker_token or not self._token_is_current(worker_token):
            msg = "worker token is not recognised on this node"
            raise PermissionError(msg)
        self.runtime.wait_for_peer(host, self.wait_seconds)
        return self.runtime.resolve_peer_host(host)

    def _token_is_current(self, worker_token: str) -> bool:
        return any(
            slot.worker_token and hmac.compare_digest(slot.worker_token, worker_token)
            for slot in self.worker_controller.active_slots()
        )


@dataclass(slots=True)
class AgentDaemonService:
    options: AgentDaemonOptions
    client: AgentGatewayClient
    state_store: AgentStateStore
    worker_controller: DockerAgentWorkerController
    resource_detector: AgentResourceDetector | None = None
    interruption_detector: AgentCapacityInterruptionDetector | None = None
    telemetry: AgentTelemetryBuffer = field(default_factory=AgentTelemetryBuffer)
    tailnet_runtime: AgentTailnetRuntime | None = None
    provider_identity: ProviderNodeIdentityEvidenceProvider | None = None
    _bootstrap_failure_reported: bool = False

    def _provider_proof(self) -> ProviderNodeIdentityEvidence:
        if self.options.provider is None:
            msg = "provider node reporting requires a provider"
            raise ValueError(msg)
        provider = self.provider_identity or provider_node_identity_evidence_provider(
            self.options.provider
        )
        return provider.create()

    def _report_bootstrap_phase(self, phase: MachineBootstrapPhase) -> None:
        """Best effort: a report must never break the boot it narrates.

        Each report mints a fresh identity proof — proofs are single-use, so
        reusing one would read as a replay and be rejected.
        """
        if not self.options.provider_enrollment_request:
            return
        with suppress(Exception):
            proof = self._provider_proof()
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
            proof = self._provider_proof()
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
        self.state_store.begin_run()
        self._report_bootstrap_phase(MachineBootstrapPhase.Booting)
        # Enrolment travels over the tailnet on a pool node, and the node's own
        # tailnet service already brought it up before this process started.
        # Nothing to attach here.
        try:
            state = self._join_step("identity.resolve", self.resolve_identity)
        except Exception:
            self._report_bootstrap_failure(MachineBootstrapFailureReason.ProviderIdentityFailed)
            raise
        self._report_bootstrap_phase(MachineBootstrapPhase.Joining)
        tailnet_runtime: AgentTailnetRuntime | None = None
        tailnet_hostname = ""
        iterations = 0
        last_result = AgentDaemonRunResult(
            workspace_id=state.workspace_id,
            pool_name=state.pool_name,
            machine_id=state.machine_id,
        )
        route_proxy: AgentRouteProxyService | None = None
        runtime_ready = False
        try:
            try:
                tailnet_runtime, tailnet_hostname = self._join_step(
                    "tailnet.start",
                    lambda: self._start_tailnet(state),
                )
            except Exception:
                self._report_bootstrap_failure(MachineBootstrapFailureReason.NetworkJoinFailed)
                raise
            last_result = last_result.model_copy(
                update={
                    "tailnet_started": tailnet_runtime is not None,
                    "tailnet_hostname": tailnet_hostname,
                }
            )
            route_proxy = self._build_route_proxy(
                state,
                tailnet_hostname=tailnet_hostname,
                tailnet_runtime=tailnet_runtime,
            )
            if route_proxy is not None:
                route_proxy.start()
                self._publish_peer_resolver(route_proxy, tailnet_runtime)
            while True:
                next_iteration = iterations + 1
                try:
                    notice = self._poll_capacity_interruption()
                    if notice is not None:
                        last_result = self._begin_capacity_interruption(
                            state,
                            notice,
                            current_iterations=next_iteration,
                            tailnet_started=tailnet_runtime is not None,
                            tailnet_hostname=tailnet_hostname,
                        )
                    else:
                        last_result = self.run_stream_iteration(
                            state,
                            current_iterations=next_iteration,
                            route_proxy=route_proxy,
                            tailnet_started=tailnet_runtime is not None,
                            tailnet_hostname=tailnet_hostname,
                        )
                except Exception as exc:
                    if agent_authority_was_revoked(exc):
                        self.worker_controller.stop_all()
                        self.state_store.mark_authority_revoked(state)
                        return last_result.model_copy(
                            update={
                                "stream_iterations": next_iteration,
                                "authority_revoked": True,
                            }
                        )
                    if self.options.once or not _recoverable_stream_error(exc):
                        raise
                    iterations = next_iteration
                    self.telemetry.enqueue_event(
                        event_type=AgentTelemetryEventType.Agent,
                        action="stream.retry",
                        status="retrying",
                        message="agent stream failed; retrying",
                        attrs={"error_type": type(exc).__name__},
                    )
                    time.sleep(self.options.stream_interval_seconds)
                    continue
                if last_result.capacity_interrupted:
                    return last_result
                if not runtime_ready:
                    self.state_store.mark_ready(state, stream_iteration=next_iteration)
                    runtime_ready = True
                iterations = next_iteration
                if self.options.once:
                    return last_result
                time.sleep(self.options.stream_interval_seconds)
        except Exception as exc:
            if not agent_authority_was_revoked(exc):
                raise
            self.worker_controller.stop_all()
            self.state_store.mark_authority_revoked(state)
            return last_result.model_copy(update={"authority_revoked": True})
        finally:
            if route_proxy is not None:
                route_proxy.close()
            if tailnet_runtime is not None:
                tailnet_runtime.close()

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
        route_proxy: AgentRouteProxyService | None = None,
        tailnet_started: bool = False,
        tailnet_hostname: str = "",
    ) -> AgentDaemonRunResult:
        stream = self.client.stream_agent(StreamAgentRequest(agent_token=state.agent_token))
        if not stream.ok:
            msg = stream.err_msg or "agent stream rejected"
            raise RuntimeError(msg)
        state = _agent_state_from_stream_response(state, stream)
        self.state_store.save(state)
        if state.capacity_state is not AgentCapacityState.Available:
            return self._resume_capacity_interruption(
                state,
                current_iterations=current_iterations,
                tailnet_started=tailnet_started,
                tailnet_hostname=tailnet_hostname,
            )
        desired_slots = [_agent_slot_from_gateway(slot) for slot in stream.slots]
        active_slots = self.worker_controller.active_slots()
        plan = plan_worker_slot_reconciliation(
            desired_slots,
            active_slots,
            executor=self.options.executor,
            os_name=agent_worker_reconcile_os(self.options.os_name),
        )
        applied = self.worker_controller.apply(
            plan,
            state.bootstrap,
            # Worker RPC takes the runtime origin, never the public one: the
            # ingress refuses `/worker-repository/*` at the edge, so a worker
            # pointed at the public origin cannot report itself available and
            # stays pending forever. Same precedence as `agent_gateway_env`.
            worker_repository_url=(
                state.bootstrap.gateway_runtime_http_url or state.sanitized_gateway_url
            ),
        )
        route_count = self._reconcile_routes(state, stream, route_proxy)
        telemetry_sent = self._send_telemetry(
            state,
            desired_worker_count=len(desired_slots),
            applied=applied,
        )
        return AgentDaemonRunResult(
            workspace_id=state.workspace_id,
            pool_name=state.pool_name,
            machine_id=state.machine_id,
            stream_iterations=current_iterations,
            route_count=route_count,
            route_proxy_target=route_proxy.proxy_target if route_proxy is not None else "",
            desired_worker_count=len(desired_slots),
            slot_action_count=len(applied),
            telemetry_sent=telemetry_sent,
            tailnet_started=tailnet_started,
            tailnet_hostname=tailnet_hostname,
        )

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
        *,
        current_iterations: int,
        tailnet_started: bool,
        tailnet_hostname: str,
    ) -> AgentDaemonRunResult:
        preempting = self._record_capacity_interruption(
            state,
            capacity_state=AgentCapacityState.Preempting,
            reason=notice.reason,
            observed_at=notice.observed_at,
            notice_at=notice.notice_at,
        )
        return self._cordon_and_stop_capacity(
            preempting,
            reason=notice.reason,
            notice_at=notice.notice_at,
            current_iterations=current_iterations,
            tailnet_started=tailnet_started,
            tailnet_hostname=tailnet_hostname,
        )

    def _resume_capacity_interruption(
        self,
        state: AgentState,
        *,
        current_iterations: int,
        tailnet_started: bool,
        tailnet_hostname: str,
    ) -> AgentDaemonRunResult:
        if state.capacity_state is AgentCapacityState.Cordoned:
            self.worker_controller.gracefully_stop_all(
                grace_seconds=self.options.interruption_grace_seconds
            )
            return _capacity_interruption_result(
                state,
                current_iterations=current_iterations,
                tailnet_started=tailnet_started,
                tailnet_hostname=tailnet_hostname,
            )
        reason = state.capacity_reason or "agent capacity interruption resumed"
        return self._cordon_and_stop_capacity(
            state,
            reason=reason,
            notice_at=state.capacity_notice_at,
            current_iterations=current_iterations,
            tailnet_started=tailnet_started,
            tailnet_hostname=tailnet_hostname,
        )

    def _cordon_and_stop_capacity(
        self,
        state: AgentState,
        *,
        reason: str,
        notice_at: datetime | None,
        current_iterations: int,
        tailnet_started: bool,
        tailnet_hostname: str,
    ) -> AgentDaemonRunResult:
        cordoned = state
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
        self.worker_controller.gracefully_stop_all(
            grace_seconds=self.options.interruption_grace_seconds
        )
        return _capacity_interruption_result(
            cordoned,
            current_iterations=current_iterations,
            tailnet_started=tailnet_started,
            tailnet_hostname=tailnet_hostname,
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
        if (
            self.options.provider is not ProviderKind.Aws
            or self.options.provider_instance_identity is not ProviderInstanceIdentityMode.ImdsV2
        ):
            raise ValueError("AWS provider node enrollment requires IMDSv2 instance identity")
        registration = self._machine_registration()
        proof_provider = self.provider_identity or provider_node_identity_evidence_provider(
            self.options.provider
        )
        proof = proof_provider.create()
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
        try:
            response = self.client.enroll_provider_node(enrollment)
        except Exception:
            self._bootstrap_failure_reported = True
            with suppress(Exception):
                failed_proof = proof_provider.create()
                self.client.record_provider_node_bootstrap_failure(
                    ProviderNodeBootstrapFailureRequest(
                        enrollment_request_id=self.options.provider_enrollment_request,
                        provider=failed_proof.provider,
                        region=failed_proof.region,
                        provider_instance_id=failed_proof.provider_instance_id,
                        identity_proof_url=failed_proof.proof_url.get_secret_value(),
                        failure_reason=MachineBootstrapFailureReason.AgentEnrollmentFailed,
                    )
                )
            raise
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

    def _reconcile_routes(
        self,
        state: AgentState,
        stream: StreamAgentResponse,
        route_proxy: AgentRouteProxyService | None,
    ) -> int:
        if route_proxy is not None:
            return route_proxy.reconcile_routes(stream.routes).tracked_routes
        ready_count = 0
        for route in stream.routes:
            if not route.route_id or not route.local_target:
                continue
            ok, latency_ms = local_target_ready(route.local_target)
            if not ok:
                continue
            self.client.update_agent_route_status(
                UpdateAgentRouteStatusRequest(
                    agent_token=state.agent_token,
                    route_id=route.route_id,
                    state=BackendRouteState.Ready,
                    proxy_target=route.proxy_target or route.local_target,
                    attrs={
                        "local_target": route.local_target,
                        "local_dial_ms": str(latency_ms),
                    },
                )
            )
            ready_count += 1
        return ready_count

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
        """
        attempt = 0
        while True:
            try:
                return operation()
            except Exception as exc:
                attempt += 1
                if attempt >= JOIN_MAX_ATTEMPTS or not _recoverable_stream_error(exc):
                    raise
                delay = min(
                    JOIN_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                    JOIN_RETRY_MAX_SECONDS,
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

    def _start_tailnet(self, state: AgentState) -> tuple[AgentTailnetRuntime | None, str]:
        if not _agent_uses_tailnet(state.bootstrap.transport):
            return (None, "")
        runtime = self.tailnet_runtime or TailnetRuntime(_tailnet_runtime_options(self.options))
        try:
            try:
                # On a pool node this is a sidecar: the node's tailnet service
                # already holds the session, so starting is a status read.
                runtime.start()
                status = runtime.status()
            except TailnetAuthenticationRequired:
                credential = self.client.request_agent_transport_credential(
                    RequestAgentTransportCredentialRequest(
                        agent_token=state.agent_token,
                        transport=BackendRouteTransport.TsnetRestricted,
                    )
                )
                status = runtime.authenticate(
                    auth_key=credential.auth_key,
                    hostname=credential.hostname,
                    control_url=credential.control_url,
                )
            _require_authenticated_tailnet_status(status)
            if not _tailnet_identity_is_this_machine(status, state.machine_id):
                # The session belongs to something other than this machine —
                # on a managed-pool node, the pool-scoped identity its bootstrap
                # used. Trade it for the single-use, machine-scoped key the
                # control plane issues now that enrolment has given this node an
                # identity, so the pool key stops being what holds it on the
                # tailnet.
                status = self._reissue_tailnet_identity(state, runtime)
            try:
                binding = self._register_tailnet_device(state, status)
            except HttpApiError as exc:
                if not _tailnet_enrollment_needs_reissue(exc):
                    raise
                # The daemon is authenticated but the control plane holds no
                # identity for it, so the two have diverged — most often because
                # the local session outlived the enrollment record. Re-enrol into
                # the identity the control plane issues and register that.
                status = self._reissue_tailnet_identity(state, runtime)
                binding = self._register_tailnet_device(state, status)
            if binding.node_id != status.self_node_id:
                raise RuntimeError("control plane returned a different tailnet node binding")
            advertise_host = _tailnet_advertise_host(status)
            return (runtime, advertise_host)
        except Exception:
            # A sidecar runtime owns no daemon, so this releases a handle rather
            # than stopping the node's tailnet.
            runtime.close()
            raise

    def _register_tailnet_device(
        self,
        state: AgentState,
        status: TailnetStatus,
    ) -> RegisterAgentTailnetDeviceResponse:
        return self.client.register_agent_tailnet_device(
            RegisterAgentTailnetDeviceRequest(
                agent_token=state.agent_token,
                node_id=status.self_node_id,
            )
        )

    def _reissue_tailnet_identity(
        self,
        state: AgentState,
        runtime: AgentTailnetRuntime,
    ) -> TailnetStatus:
        """Take a fresh identity from the control plane and adopt it locally.

        Forced, because the daemon already holds a session; without replacing it
        the agent would re-register the same device the control plane just
        rejected.
        """
        credential = self.client.request_agent_transport_credential(
            RequestAgentTransportCredentialRequest(
                agent_token=state.agent_token,
                transport=BackendRouteTransport.TsnetRestricted,
            )
        )
        status = runtime.authenticate(
            auth_key=credential.auth_key,
            hostname=credential.hostname,
            control_url=credential.control_url,
            force=True,
        )
        _require_authenticated_tailnet_status(status)
        return status

    def _publish_peer_resolver(
        self,
        route_proxy: AgentRouteProxyService,
        tailnet_runtime: AgentTailnetRuntime | None,
    ) -> None:
        """Tell workers where to ask for peers, once there is somewhere to ask.

        Both values travel together: an address without a suffix resolves
        nothing, and a suffix without an address names peers a worker cannot
        look up. Absent either, the worker dials names as written.
        """
        if tailnet_runtime is None:
            return
        try:
            suffix = _tailnet_dns_suffix(tailnet_runtime.status())
        except Exception:
            LOGGER.debug("tailnet status unavailable; peers stay dialled by name", exc_info=True)
            return
        if not suffix or not route_proxy.proxy_target:
            return
        self.worker_controller.peer_resolver_address = route_proxy.proxy_target
        self.worker_controller.tailnet_dns_suffix = suffix

    def _build_route_proxy(
        self,
        state: AgentState,
        *,
        tailnet_hostname: str = "",
        tailnet_runtime: AgentTailnetRuntime | None = None,
    ) -> AgentRouteProxyService | None:
        if not self.options.route_proxy.enabled:
            return None
        config = self.options.route_proxy
        if tailnet_hostname and _agent_uses_tailnet(state.bootstrap.transport):
            update: dict[str, str] = {}
            if not config.advertise_host:
                update["advertise_host"] = tailnet_hostname
            if config.bind_host == "127.0.0.1":
                update["bind_host"] = tailnet_hostname
            if update:
                config = config.model_copy(update=update)
        resolver = (
            WorkerTailnetPeerResolver(tailnet_runtime, self.worker_controller)
            if tailnet_runtime is not None
            else None
        )
        return AgentRouteProxyService(
            config,
            self.client,
            state.agent_token,
            telemetry=self.telemetry,
            peer_resolver=resolver,
        )


def build_agent_daemon_service(
    options: AgentDaemonOptions,
    *,
    client: AgentGatewayClient | None = None,
    worker_controller: DockerAgentWorkerController | None = None,
    resource_detector: AgentResourceDetector | None = None,
    interruption_detector: AgentCapacityInterruptionDetector | None = None,
    tailnet_runtime: AgentTailnetRuntime | None = None,
    provider_identity: ProviderNodeIdentityEvidenceProvider | None = None,
) -> AgentDaemonService:
    state_dir = Path(options.state_dir)
    return AgentDaemonService(
        options=options,
        client=client or HttpAgentGatewayClient.from_options(options),
        state_store=AgentStateStore(state_dir),
        worker_controller=worker_controller
        or DockerAgentWorkerController(
            state_dir,
            docker_binary=options.docker_binary,
            worker_image_override=options.worker_image,
            worker_runtime_http_url_override=options.worker_runtime_http_url,
            target_host=options.worker_route_target,
            worker_network=options.worker_network,
            host_aliases=_worker_host_aliases(options),
            platform=agent_worker_platform(options.os_name, options.arch),
        ),
        resource_detector=resource_detector,
        interruption_detector=(
            interruption_detector or _provider_capacity_interruption_detector(options)
        ),
        tailnet_runtime=tailnet_runtime,
        provider_identity=provider_identity,
    )


def _provider_capacity_interruption_detector(
    options: AgentDaemonOptions,
) -> AgentCapacityInterruptionDetector | None:
    if (
        options.provider is not ProviderKind.Aws
        or options.provider_instance_identity is not ProviderInstanceIdentityMode.ImdsV2
    ):
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
            notice_at=notice.notice_at,
        )

    return detect


def _tailnet_runtime_options(
    options: AgentDaemonOptions,
) -> TailnetRuntimeOptions:
    state_dir = options.tailnet_state_dir or str(Path(options.state_dir) / AGENT_TAILNET_DIR_NAME)
    return TailnetRuntimeOptions(
        mode=options.tailnet_mode,
        state_dir=state_dir,
        socket_path=options.tailnet_socket_path,
        tailscale_binary=options.tailnet_tailscale_binary,
        tailscaled_binary=options.tailnet_tailscaled_binary,
        # The node reaches the control plane and the cache as tailnet peers, so
        # it has to resolve tailnet names. Declining the tailnet's DNS left the
        # VPC resolver answering `*.ts.net` from public records that point at
        # Tailscale's own infrastructure rather than the peer, so every lookup
        # succeeded and every connection to it timed out. Non-tailnet queries
        # are forwarded upstream unchanged.
        accept_dns=True,
        accept_routes=False,
        userspace_networking=options.tailnet_userspace_networking,
    )


def _tailnet_advertise_host(status: TailnetStatus) -> str:
    for tailnet_ip in status.tailnet_ips:
        if "." in tailnet_ip:
            return tailnet_ip
    if status.tailnet_ips:
        return status.tailnet_ips[0]
    return status.self_dns_name.strip().rstrip(".") or status.self_host_name.strip().rstrip(".")


def _tailnet_dns_suffix(status: TailnetStatus) -> str:
    """The tailnet's DNS suffix, taken from this node's own name.

    Derived rather than configured: the node already knows which tailnet it
    joined, and a separately stated suffix is one more value that can disagree
    with reality.
    """
    dns_name = status.self_dns_name.strip().rstrip(".")
    _host, _, suffix = dns_name.partition(".")
    return suffix


def _tailnet_device_hostname(status: TailnetStatus) -> str:
    return status.self_host_name.strip().rstrip(".") or status.self_dns_name.strip().rstrip(".")


def _tailnet_identity_is_this_machine(status: TailnetStatus, machine_id: str) -> bool:
    """Whether the local session is one the control plane issued to this machine.

    The generation is deliberately not matched: the agent does not know which
    generation the control plane last handed out, and demanding an exact name
    would rotate the identity on every restart. The prefix is enough to tell
    this machine's session from another machine's, which is the distinction
    that decides whether a rotation is owed.
    """
    machine = machine_id.strip()
    if not machine:
        return False
    return _tailnet_device_hostname(status).startswith(f"{AGENT_NAME}-{machine}-g")


def _require_authenticated_tailnet_status(status: TailnetStatus) -> None:
    if not status.self_node_id.strip():
        raise RuntimeError("authenticated tailnet status is missing the stable node ID")
    if not _tailnet_device_hostname(status):
        raise RuntimeError("authenticated tailnet status is missing the device hostname")
    if not status.tailnet_ips:
        raise RuntimeError("authenticated tailnet status is missing a tailnet IP")


def _agent_uses_tailnet(transport: str) -> bool:
    try:
        return normalize_backend_route_transport(transport) is BackendRouteTransport.TsnetRestricted
    except ValueError:
        normalized = transport.strip().lower().replace("-", "_")
        return normalized in {"tailnet", "tsnet", "tailscale", "tsnet_restricted"}


def _worker_host_aliases(options: AgentDaemonOptions) -> list[str]:
    aliases = list(options.worker_host_aliases)
    if options.worker_route_target.strip().lower() == "host.docker.internal":
        aliases.append("host.docker.internal:host-gateway")
    return list(dict.fromkeys(aliases))


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
        pool_name=slot.pool_name,
        capacity_owner_id=slot.capacity_owner_id,
        machine_id=slot.machine_id,
        cpu_millicores=slot.cpu,
        memory_mb=slot.memory,
        gpu=slot.gpu,
        gpu_count=slot.gpu_count,
        gpu_assignment=slot.gpu_assignment,
        network_prefix=slot.network_prefix,
        worker_image=slot.worker_image,
    )


def _agent_bootstrap(
    config: AgentBootstrapConfig | None,
    *,
    fallback_gateway_url: str,
) -> AgentBootstrap:
    if config is None:
        return AgentBootstrap(
            gateway_public_http_url=fallback_gateway_url,
            gateway_runtime_http_url=fallback_gateway_url,
        )
    return AgentBootstrap(
        gateway_public_http_url=config.gateway_public_http_url or fallback_gateway_url,
        gateway_runtime_http_url=config.gateway_runtime_http_url or fallback_gateway_url,
        gateway_grpc_host=config.gateway_grpc_host,
        gateway_grpc_port=config.gateway_grpc_port,
        gateway_grpc_tls=config.gateway_grpc_tls,
        transport=config.transport.value,
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
        pool_name=response.pool_name,
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
    if (
        not response.credential_id
        or response.credential_id != state.credential_id
        or response.credential_generation != state.credential_generation
    ):
        raise RuntimeError("gateway returned the wrong agent stream session")
    return state.model_copy(
        update={
            "capacity_state": response.capacity_state,
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
    tailnet_started: bool,
    tailnet_hostname: str,
) -> AgentDaemonRunResult:
    return AgentDaemonRunResult(
        workspace_id=state.workspace_id,
        pool_name=state.pool_name,
        machine_id=state.machine_id,
        stream_iterations=current_iterations,
        tailnet_started=tailnet_started,
        tailnet_hostname=tailnet_hostname,
        capacity_state=state.capacity_state,
        capacity_interrupted=True,
    )


def _payload(model: ContractModel) -> dict[str, JsonValue]:
    return _JSON_OBJECT_ADAPTER.validate_json(model.model_dump_json())


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


def _tailnet_enrollment_needs_reissue(exc: HttpApiError) -> bool:
    return (
        400 <= exc.status_code < 500
        and (exc.detail or "").strip() == TAILNET_ENROLLMENT_NOT_AWAITING_DETAIL
    )


def _recoverable_stream_error(exc: Exception) -> bool:
    # A diverged enrollment is a state mismatch rather than a rejection of the
    # agent's authority, so it must not end the process; the next join re-enrols.
    if isinstance(exc, HttpApiError) and _tailnet_enrollment_needs_reissue(exc):
        return True
    if isinstance(exc, HttpApiError):
        return exc.status_code >= 500
    # A transport error means the request never reached a response, so the
    # control plane has not rejected anything and retrying is always correct.
    # These arrive as HttpTransportError, which is a plain RuntimeError, so it
    # has to be named explicitly or every TLS reset reads as a fatal error.
    if isinstance(exc, HttpTransportError):
        return True
    return isinstance(
        exc,
        (
            ConnectionError,
            TimeoutError,
            OSError,
            http_client.HTTPException,
            urllib.error.URLError,
        ),
    )


def agent_authority_was_revoked(exc: Exception) -> bool:
    if not isinstance(exc, HttpApiError) or not 400 <= exc.status_code < 500:
        return False
    detail = (exc.detail or str(exc)).strip().lower()
    return detail in AGENT_AUTHORITY_REVOKED_DETAILS


def _write_json_atomic(path: Path, payload: JsonValue, *, permissions: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.chmod(permissions)
    os.replace(tmp_path, path)


def _write_worker_configuration_atomic(
    path: Path,
    config: WorkerConfiguration,
    *,
    permissions: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp_path.open("w", encoding="utf-8") as file:
            file.write(serialize_worker_configuration(config))
            file.flush()
            os.fsync(file.fileno())
        tmp_path.chmod(permissions)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
