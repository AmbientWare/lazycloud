from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from foundation.process import ProcessOutputSink
from pydantic import Field
from shared.container_requests import CONTAINER_INNER_PORT, WorkerStartupKind
from shared.contracts import ContractModel
from shared.env import GATEWAY_TOKEN_ENV
from shared.image_building.authoring import LinuxArchitecture
from shared.realtime.contracts import CloudEventRecord
from shared.scheduling import (
    DEFAULT_CONTAINER_STATE_TTL_SECONDS,
    SchedulerContainerStatus,
)
from shared.worker_events import WorkerEventRecord
from storage_client.mounts import StorageMountResult

from worker.container_logs import ContainerLogCaptureResult
from worker.container_rootfs import (
    ContainerRootfsReleaseResult,
    ContainerRootfsSetupResult,
    ContainerRootfsStatus,
)
from worker.events import (
    ContainerEventPayload,
    ContainerExecutionPhase,
    ContainerLifecyclePayload,
    ContainerRequestContext,
    StopContainerReason,
    container_exit_reason,
    container_lifecycle_from_duration,
    event_stop_reason,
    normalize_container_exit_code,
    populate_container_event,
)
from worker.execution import (
    MIB,
    ContainerNetworkIdentity,
    ContainerResourceRequest,
    OciMount,
    PortBinding,
    WorkerOomWatcherPlan,
    plan_oci_linux_resources,
    select_worker_oom_watcher,
)
from worker.finalization import (
    ContainerFinalizationRequest,
    ContainerFinalizationResult,
    ContainerStatusUpdater,
    WorkerContainerFinalizationService,
)
from worker.gpu import ContainerGpuAssignmentResult
from worker.lifecycle import (
    ContainerStartupPortRequest,
    PreparedRequestMount,
    RequestMountLinkPlan,
    plan_startup_port_bindings,
    startup_container_ports,
)
from worker.monitoring import (
    ContainerRuntimeMonitor,
    ContainerRuntimeMonitorHandle,
    ContainerRuntimeMonitoringResult,
)
from worker.oci_spec import OciRuntimeContainerSpec
from worker.runtime_config import (
    OciRuntimeName,
    apply_unsupported_cgroup_parameters,
)
from worker.supervision import WorkerOomHandlingResult, WorkerSupervisionService

LOGGER = logging.getLogger(__name__)

CONTAINER_EXIT_EVENT_ID = "container.exited"
CONTAINER_EXIT_MESSAGE = "container process exited"


class WorkerAddressPublisher(Protocol):
    def publish_worker_address(
        self,
        request: ContainerRequestContext,
    ) -> ContractModel | None: ...


class ContainerImageLoader(Protocol):
    def load_image(self, request: ContainerRequestContext) -> ContainerImageLoadResult: ...


class ContainerCredentialHydrator(Protocol):
    def hydrate_container_credentials(
        self,
        context: ContainerExecutionContext,
    ) -> ContainerExecutionContext: ...


class ContainerPortAllocator(Protocol):
    def allocate_ports(self, count: int) -> list[int]: ...


class ContainerRoutePublisher(Protocol):
    def publish_container_routes(
        self,
        context: ContainerExecutionContext,
        *,
        port_bindings: list[PortBinding],
    ) -> ContractModel | None: ...


class ContainerNetworkPreparer(Protocol):
    def setup_network(
        self,
        context: ContainerExecutionContext,
        *,
        port_bindings: list[PortBinding],
    ) -> ContainerNetworkSetupResult: ...


class ContainerMountPreparer(Protocol):
    def setup_mounts(self, request: ContainerRequestContext) -> ContainerMountSetupResult: ...


class ContainerRootfsPreparer(Protocol):
    def prepare(
        self,
        *,
        container_id: str,
        image_id: str,
        disk_limit_bytes: int = 0,
    ) -> ContainerRootfsSetupResult: ...

    def release(self, container_id: str) -> ContainerRootfsReleaseResult: ...


class ContainerWorkspaceStorageMounter(Protocol):
    def ensure_workspace_storage(
        self,
        request: ContainerRequestContext,
    ) -> ContractModel | None: ...

    def cleanup_unused(
        self,
        *,
        active_workspace_names: set[str],
    ) -> list[StorageMountResult]: ...


class ContainerGpuAssigner(Protocol):
    def assign_gpus(self, request: ContainerRequestContext) -> ContainerGpuAssignmentResult: ...

    def release_gpu(self, container_id: str) -> None: ...


class ContainerSpecBuilder(Protocol):
    def build_spec(
        self,
        context: ContainerExecutionContext,
        *,
        bind_ports: list[int],
        port_bindings: list[PortBinding],
        mount_result: ContainerMountSetupResult,
        network_result: ContainerNetworkSetupResult | None = None,
        gpu_result: ContainerGpuAssignmentResult | None = None,
        rootfs_result: ContainerRootfsSetupResult | None = None,
    ) -> OciRuntimeContainerSpec: ...


class ContainerRuntimeExecutor(Protocol):
    def prepare(self, spec: OciRuntimeContainerSpec) -> None: ...

    def run(
        self,
        spec: OciRuntimeContainerSpec,
        *,
        on_started: Callable[[int], None],
        output_sink: ProcessOutputSink | None = None,
    ) -> ContainerRuntimeRunResult: ...


class ContainerCheckpointRestorer(Protocol):
    def restore(
        self,
        context: ContainerExecutionContext,
        spec: OciRuntimeContainerSpec,
        *,
        on_started: Callable[[int], None],
        output_sink: ProcessOutputSink | None = None,
    ) -> ContainerRuntimeRunResult | None: ...


class ContainerLogCaptureHandle(Protocol):
    @property
    def process_output_sink(self) -> ProcessOutputSink: ...

    def record_diagnostic(self, message: str) -> None: ...

    def close(self, *, timeout_seconds: float | None = None) -> ContainerLogCaptureResult: ...


class ContainerLogCaptureService(Protocol):
    def begin(self, request: ContainerRequestContext) -> ContainerLogCaptureHandle: ...


class ContainerAutomaticCheckpointCoordinator(Protocol):
    def prepare_mount(
        self,
        context: ContainerExecutionContext,
        mount_result: ContainerMountSetupResult,
    ) -> ContainerMountSetupResult: ...

    def checkpoint_or_complete_restore(
        self,
        context: ContainerExecutionContext,
        *,
        container_hostname: str,
    ) -> str: ...

    def cleanup(self, container_id: str) -> None: ...


class ContainerInstanceRecorder(Protocol):
    def record_container_instance(
        self,
        context: ContainerExecutionContext,
        *,
        spec: OciRuntimeContainerSpec,
        mount_result: ContainerMountSetupResult,
        network_result: ContainerNetworkSetupResult | None,
        port_bindings: list[PortBinding],
    ) -> None: ...


class ContainerSandboxDockerPreparer(Protocol):
    def prepare(self, container_id: str) -> None: ...


class ContainerExitEventPublisher(Protocol):
    def publish_container_exit(
        self,
        payload: ContainerEventPayload,
    ) -> WorkerEventRecord | None: ...


class ContainerLifecyclePublisher(Protocol):
    def publish_container_lifecycle(
        self,
        payload: ContainerLifecyclePayload,
    ) -> CloudEventRecord | None: ...


class ContainerImageLoadResult(ContractModel):
    loaded: bool = True
    short_circuit_exit_code: int | None = None
    reason: str = ""


class ContainerMountSetupResult(ContractModel):
    mounts: list[PreparedRequestMount] = Field(default_factory=list)
    oci_mounts: list[OciMount] = Field(default_factory=list)
    volume_cache_map: dict[str, str] = Field(default_factory=dict)
    source_dirs_to_create: list[str] = Field(default_factory=list)
    symlinks: list[RequestMountLinkPlan] = Field(default_factory=list)
    reason: str = ""


class ContainerNetworkSetupResult(ContractModel):
    enabled: bool = False
    identity: ContainerNetworkIdentity | None = None
    namespace_path: str = ""
    veth_host: str = ""
    veth_container: str = ""
    operations: list[str] = Field(default_factory=list)
    reason: str = ""


class ContainerRuntimeRunResult(ContractModel):
    exit_code: int
    stop_reason: StopContainerReason = StopContainerReason.Unknown
    oom_killed: bool = False
    started_pid: int | None = None
    output: str = ""


class ContainerRuntimeStartError(RuntimeError):
    def __init__(
        self,
        container_id: str,
        *,
        exit_code: int,
        output: str,
    ) -> None:
        super().__init__(
            f"runtime exited during start callback for {container_id} with exit code {exit_code}"
        )
        self.container_id = container_id
        self.exit_code = exit_code
        self.output = output


class ContainerExecutionContext(ContractModel):
    request: ContainerRequestContext
    architecture: LinuxArchitecture = LinuxArchitecture.Amd64
    startup_kind: WorkerStartupKind = WorkerStartupKind.Unknown
    ports: list[int] = Field(default_factory=list)
    requested_ports: list[int] = Field(default_factory=list)
    checkpoint_exposed_ports: list[int] = Field(default_factory=list)
    checkpoint_id: str = ""
    checkpoint_enabled: bool = False
    checkpoint_readiness_path: str = ""
    checkpoint_readiness_port: int = 0
    checkpoint_readiness_timeout_seconds: int = 600
    checkpoint_readiness_interval_seconds: float = 1.0
    entrypoint: list[str] = Field(default_factory=list)
    cwd: str = "/workspace"
    runtime: OciRuntimeName = OciRuntimeName.Runsc
    docker_enabled: bool = False
    block_network: bool = False
    allow_list: list[str] = Field(default_factory=list)
    memory_enforced: bool = True
    memory_limit_bytes: int | None = None
    cpu_limit_millicores: int = 0
    node_cpu_millicores: int = 0
    node_memory_mib: int = 0
    """What the machine has, or zero when it could not be read."""

    cgroup_path: str | None = None
    run_delayed_cleanup: bool = False


class ContainerExecutionPhaseResult(ContractModel):
    phase: ContainerExecutionPhase
    ok: bool = True
    skipped: bool = False
    error_message: str = ""


class ContainerExecutionResult(ContractModel):
    phases: list[ContainerExecutionPhaseResult] = Field(default_factory=list)
    image_result: ContainerImageLoadResult | None = None
    image_loaded: bool = False
    bind_ports: list[int] = Field(default_factory=list)
    port_bindings: list[PortBinding] = Field(default_factory=list)
    network_result: ContainerNetworkSetupResult | None = None
    mount_result: ContainerMountSetupResult | None = None
    gpu_result: ContainerGpuAssignmentResult | None = None
    rootfs_result: ContainerRootfsSetupResult | None = None
    oom_watcher: WorkerOomWatcherPlan | None = None
    oom_result: WorkerOomHandlingResult | None = None
    monitoring: ContainerRuntimeMonitoringResult | None = None
    finalization: ContainerFinalizationResult | None = None
    delayed_cleanup: ContainerFinalizationResult | None = None
    exit_event: ContainerEventPayload | None = None
    exit_code: int | None = None
    runtime_output: str = ""
    container_logs: ContainerLogCaptureResult | None = None
    lifecycle_errors: list[str] = Field(default_factory=list)
    automatic_checkpoint_id: str = ""

    @property
    def ok(self) -> bool:
        return all(phase.ok for phase in self.phases)

    @property
    def failed_phase(self) -> ContainerExecutionPhase | None:
        for phase in self.phases:
            if not phase.ok:
                return phase.phase
        return None


def container_resource_request(context: ContainerExecutionContext) -> ContainerResourceRequest:
    """One reading of a container's resource ask, for everything that needs it.

    The spec, the OOM watcher and the deferred cgroup write all have to agree on
    the same numbers; deriving them separately is how the watcher ended up
    guarding a ceiling the cgroup did not enforce.
    """
    return ContainerResourceRequest(
        cpu_millicores=context.request.cpu_millicores,
        memory_mib=context.request.memory_mib,
        memory_enforced=context.memory_enforced,
        cpu_limit_millicores=context.cpu_limit_millicores,
        memory_limit_mib=(context.memory_limit_bytes or 0) // MIB,
        node_cpu_millicores=context.node_cpu_millicores,
        node_memory_mib=context.node_memory_mib,
    )


def enforced_memory_limit_bytes(context: ContainerExecutionContext) -> int | None:
    """The wall the cgroup holds, which is what anything watching must watch."""
    if context.request.cpu_millicores <= 0 or context.request.memory_mib <= 0:
        return context.memory_limit_bytes
    resources = plan_oci_linux_resources(container_resource_request(context))
    if resources.memory is None:
        return context.memory_limit_bytes
    return resources.memory.limit_bytes


@dataclass(slots=True)
class WorkerContainerExecutionService:
    address_publisher: WorkerAddressPublisher
    image_loader: ContainerImageLoader
    port_allocator: ContainerPortAllocator
    mount_preparer: ContainerMountPreparer
    rootfs_preparer: ContainerRootfsPreparer
    spec_builder: ContainerSpecBuilder
    runtime: ContainerRuntimeExecutor
    finalizer: WorkerContainerFinalizationService
    route_publisher: ContainerRoutePublisher | None = None
    network_preparer: ContainerNetworkPreparer | None = None
    workspace_storage_mounter: ContainerWorkspaceStorageMounter | None = None
    gpu_assigner: ContainerGpuAssigner | None = None
    instance_recorder: ContainerInstanceRecorder | None = None
    sandbox_docker_preparer: ContainerSandboxDockerPreparer | None = None
    credential_hydrator: ContainerCredentialHydrator | None = None
    oom_supervisor: WorkerSupervisionService | None = None
    exit_events: ContainerExitEventPublisher | None = None
    lifecycle_events: ContainerLifecyclePublisher | None = None
    runtime_monitor: ContainerRuntimeMonitor | None = None
    status_repository: ContainerStatusUpdater | None = None
    checkpoint_restorer: ContainerCheckpointRestorer | None = None
    automatic_checkpoints: ContainerAutomaticCheckpointCoordinator | None = None
    container_logs: ContainerLogCaptureService | None = None
    container_started: Callable[[str, int], None] | None = None
    """Told the sandbox process id the moment a container has one.

    A long-running container's pid is not known when it is registered and is not
    reported again until it exits, so anything watching live containers has to be
    handed it here or it never learns of the container at all.
    """

    def _apply_deferred_cgroup_parameters(self, context: ContainerExecutionContext) -> None:
        """Install the cgroup settings the runtime dropped.

        runsc ignores `linux.resources.unified`, so a container starts with
        `memory.high` at `max` and `memory.oom.group` at `0` however the spec was
        written. Everything under `resources.memory` it does apply, so this is
        the whole of what has to be written by hand.

        A failure is reported rather than raised: the container is already
        running under the limits runsc did install, and killing it for a missing
        throttle would be a worse outcome than running without one.
        """
        if context.request.cpu_millicores <= 0 or context.request.memory_mib <= 0:
            return
        deferred = plan_oci_linux_resources(container_resource_request(context)).deferred
        if not deferred:
            return
        written = apply_unsupported_cgroup_parameters(context.request.container_id, deferred)
        missing = sorted(set(deferred) - set(written))
        if missing:
            LOGGER.warning(
                "container %s is running without cgroup settings: %s",
                context.request.container_id,
                ", ".join(missing),
            )

    def execute(self, context: ContainerExecutionContext) -> ContainerExecutionResult:
        result = ContainerExecutionResult()
        if not self._phase(
            result,
            ContainerExecutionPhase.PublishWorkerAddress,
            lambda: self.address_publisher.publish_worker_address(context.request),
            request=context.request,
        ):
            self._finalize_startup_failure(result, context)
            return result

        if self.credential_hydrator is not None:
            context_holder = {"context": context}
            if not self._phase(
                result,
                ContainerExecutionPhase.HydrateCredentials,
                lambda: self._hydrate_credentials(context_holder),
                request=context.request,
            ):
                self._finalize_startup_failure(result, context)
                return result
            context = context_holder["context"]

        if not self._phase(
            result,
            ContainerExecutionPhase.LoadImage,
            lambda: self._set_image_result(context, result),
            request=context.request,
        ):
            self._finalize_startup_failure(result, context)
            return result
        image_result = result.image_result or ContainerImageLoadResult(loaded=result.image_loaded)
        if not result.image_loaded:
            return result
        if image_result.short_circuit_exit_code is not None:
            self._finalize(
                result,
                context,
                exit_code=image_result.short_circuit_exit_code,
                stop_reason=StopContainerReason.Unknown,
                oom_killed=False,
            )
            return result

        ports = startup_container_ports(
            ContainerStartupPortRequest(
                kind=context.startup_kind,
                ports=context.ports,
                requested_ports=context.requested_ports,
                checkpoint_exposed_ports=context.checkpoint_exposed_ports,
            )
        )
        if not self._phase(
            result,
            ContainerExecutionPhase.AllocatePorts,
            lambda: self._allocate_ports(result, ports),
            request=context.request,
        ):
            self._finalize_startup_failure(result, context)
            return result
        startup_bindings = plan_startup_port_bindings(
            ContainerStartupPortRequest(
                kind=context.startup_kind,
                ports=ports,
                requested_ports=context.requested_ports,
                checkpoint_exposed_ports=context.checkpoint_exposed_ports,
            ),
            bind_ports=result.bind_ports,
        )
        result.port_bindings = list(startup_bindings.bindings)

        network_result_holder: dict[str, ContainerNetworkSetupResult] = {}
        if not self._phase(
            result,
            ContainerExecutionPhase.SetupNetwork,
            lambda: self._set_network_result(context, result, network_result_holder),
            skip=self.network_preparer is None,
            request=context.request,
        ):
            self._finalize_startup_failure(result, context)
            return result
        network_result = network_result_holder.get("network_result")
        if not self._phase(
            result,
            ContainerExecutionPhase.SetupWorkspaceStorage,
            lambda: self._ensure_workspace_storage(context),
            skip=self.workspace_storage_mounter is None,
            request=context.request,
        ):
            self._finalize_startup_failure(result, context)
            return result

        mount_result_holder: dict[str, ContainerMountSetupResult] = {}
        if not self._phase(
            result,
            ContainerExecutionPhase.SetupMounts,
            lambda: self._set_mount_result(context, result, mount_result_holder),
            request=context.request,
        ):
            self._finalize_startup_failure(result, context)
            return result
        mount_result = mount_result_holder["mount_result"]

        if not self._phase(
            result,
            ContainerExecutionPhase.PrepareRootfs,
            lambda: self._set_rootfs_result(context, result),
            request=context.request,
        ):
            self._finalize_startup_failure(result, context)
            return result

        gpu_result_holder: dict[str, ContainerGpuAssignmentResult] = {}
        if not self._phase(
            result,
            ContainerExecutionPhase.AssignGpu,
            lambda: self._set_gpu_result(context, result, gpu_result_holder),
            skip=self.gpu_assigner is None or context.request.gpu_count <= 0,
            request=context.request,
        ):
            self._finalize_startup_failure(result, context)
            return result
        gpu_result = gpu_result_holder.get("gpu_result")

        spec_holder: dict[str, OciRuntimeContainerSpec] = {}
        if not self._phase(
            result,
            ContainerExecutionPhase.BuildSpec,
            lambda: self._set_spec(
                context,
                result,
                spec_holder,
                result.bind_ports,
                mount_result,
                network_result,
                gpu_result,
                result.rootfs_result,
            ),
            request=context.request,
        ):
            self._finalize_startup_failure(result, context)
            return result
        spec = spec_holder["spec"]

        if not self._phase(
            result,
            ContainerExecutionPhase.PrepareRuntime,
            lambda: self._prepare_runtime(
                context,
                spec=spec,
                mount_result=mount_result,
                network_result=network_result,
                port_bindings=result.port_bindings,
            ),
            request=context.request,
        ):
            self._finalize_startup_failure(result, context)
            return result

        started_pid: int | None = None

        def on_started(pid: int) -> None:
            nonlocal started_pid
            started_pid = pid
            if self.container_started is not None:
                self.container_started(context.request.container_id, pid)
            # Written here because the cgroup does not exist until the runtime
            # has made one, and runsc will not install these from the spec.
            self._apply_deferred_cgroup_parameters(context)
            if not self._phase(
                result,
                ContainerExecutionPhase.PrepareSandboxDocker,
                lambda: self._prepare_sandbox_docker(context),
                skip=not context.docker_enabled,
                request=context.request,
            ):
                msg = result.phases[-1].error_message
                raise RuntimeError(msg)
            if not self._phase(
                result,
                ContainerExecutionPhase.CompleteCheckpointStartup,
                lambda: self._complete_checkpoint_startup(context, result),
                skip=not context.checkpoint_enabled and not context.checkpoint_id,
                request=context.request,
            ):
                msg = result.phases[-1].error_message
                raise RuntimeError(msg)
            if not self._mark_running(result, context):
                msg = result.phases[-1].error_message
                raise RuntimeError(msg)
            result.oom_watcher = select_worker_oom_watcher(
                context.runtime,
                pid=pid,
                memory_enforced=context.memory_enforced,
                # The ceiling the cgroup actually enforces, not the one asked
                # for. Watching the larger figure means the kernel reaches its
                # wall first and the attribution this watcher exists to produce
                # is never made.
                memory_limit_bytes=enforced_memory_limit_bytes(context),
                cgroup_path=context.cgroup_path,
            )
            if not self._phase(
                result,
                ContainerExecutionPhase.PublishContainerRoutes,
                lambda: self._publish_container_routes(context, result),
                skip=self.route_publisher is None,
                request=context.request,
            ):
                msg = result.phases[-1].error_message
                raise RuntimeError(msg)

        run_result_holder: dict[str, ContainerRuntimeRunResult] = {}
        if not self._phase(
            result,
            ContainerExecutionPhase.RunRuntime,
            lambda: self._run_runtime(context, result, spec, on_started, run_result_holder),
            request=context.request,
        ):
            failed_phase, detail = _first_phase_failure(result)
            self._finalize(
                result,
                context,
                exit_code=1,
                stop_reason=StopContainerReason.Unknown,
                oom_killed=False,
                failed_phase=failed_phase,
                failure_detail=(_redact_runtime_output(detail, context.request) if detail else ""),
            )
            return result
        run_result = run_result_holder["run_result"]
        if run_result.output:
            result.runtime_output = run_result.output
        if started_pid is None and run_result.started_pid is not None:
            on_started(run_result.started_pid)

        if run_result.oom_killed and self.oom_supervisor is not None and result.oom_watcher:
            self._phase(
                result,
                ContainerExecutionPhase.HandleOom,
                lambda: self._handle_oom(result, context),
                request=context.request,
            )

        mapped_exit_code = normalize_container_exit_code(
            run_result.exit_code,
            stop_reason=run_result.stop_reason,
            oom_killed=run_result.oom_killed,
        )
        result.exit_code = mapped_exit_code
        self._phase(
            result,
            ContainerExecutionPhase.PublishExitEvent,
            lambda: self._publish_exit_event(
                result,
                context,
                raw_exit_code=run_result.exit_code,
                mapped_exit_code=mapped_exit_code,
                stop_reason=run_result.stop_reason,
                oom_killed=run_result.oom_killed,
            ),
            skip=self.exit_events is None,
            request=context.request,
        )
        self._finalize(
            result,
            context,
            exit_code=run_result.exit_code,
            stop_reason=run_result.stop_reason,
            oom_killed=run_result.oom_killed,
        )
        return result

    def _set_image_result(
        self,
        context: ContainerExecutionContext,
        result: ContainerExecutionResult,
    ) -> None:
        loaded = self.image_loader.load_image(context.request)
        result.image_result = loaded
        result.image_loaded = loaded.loaded
        if loaded.short_circuit_exit_code is not None:
            result.exit_code = loaded.short_circuit_exit_code

    def _hydrate_credentials(
        self,
        holder: dict[str, ContainerExecutionContext],
    ) -> None:
        if self.credential_hydrator is None:
            return
        holder["context"] = self.credential_hydrator.hydrate_container_credentials(
            holder["context"]
        )

    def _allocate_ports(self, result: ContainerExecutionResult, ports: list[int]) -> None:
        result.bind_ports = self.port_allocator.allocate_ports(len(ports))

    def _set_mount_result(
        self,
        context: ContainerExecutionContext,
        result: ContainerExecutionResult,
        holder: dict[str, ContainerMountSetupResult],
    ) -> None:
        mount_result = self.mount_preparer.setup_mounts(context.request)
        if context.checkpoint_enabled or context.checkpoint_id:
            if self.automatic_checkpoints is None:
                raise RuntimeError("automatic checkpoint coordination is not configured")
            mount_result = self.automatic_checkpoints.prepare_mount(context, mount_result)
        result.mount_result = mount_result
        holder["mount_result"] = mount_result

    def _ensure_workspace_storage(self, context: ContainerExecutionContext) -> None:
        if self.workspace_storage_mounter is None:
            return
        self.workspace_storage_mounter.ensure_workspace_storage(context.request)

    def _set_network_result(
        self,
        context: ContainerExecutionContext,
        result: ContainerExecutionResult,
        holder: dict[str, ContainerNetworkSetupResult],
    ) -> None:
        if self.network_preparer is None:
            return
        network_result = self.network_preparer.setup_network(
            context,
            port_bindings=result.port_bindings,
        )
        result.network_result = network_result
        holder["network_result"] = network_result

    def _set_gpu_result(
        self,
        context: ContainerExecutionContext,
        result: ContainerExecutionResult,
        holder: dict[str, ContainerGpuAssignmentResult],
    ) -> None:
        if self.gpu_assigner is None:
            return
        gpu_result = self.gpu_assigner.assign_gpus(context.request)
        result.gpu_result = gpu_result
        holder["gpu_result"] = gpu_result
        if not gpu_result.ok:
            raise RuntimeError(gpu_result.error_message or "gpu assignment failed")

    def _set_spec(
        self,
        context: ContainerExecutionContext,
        result: ContainerExecutionResult,
        holder: dict[str, OciRuntimeContainerSpec],
        bind_ports: list[int],
        mount_result: ContainerMountSetupResult,
        network_result: ContainerNetworkSetupResult | None,
        gpu_result: ContainerGpuAssignmentResult | None,
        rootfs_result: ContainerRootfsSetupResult | None = None,
    ) -> None:
        holder["spec"] = self.spec_builder.build_spec(
            context,
            bind_ports=bind_ports,
            port_bindings=result.port_bindings,
            mount_result=mount_result,
            network_result=network_result,
            gpu_result=gpu_result,
            rootfs_result=rootfs_result,
        )

    def _set_rootfs_result(
        self,
        context: ContainerExecutionContext,
        result: ContainerExecutionResult,
    ) -> None:
        rootfs_result = self.rootfs_preparer.prepare(
            container_id=context.request.container_id,
            image_id=context.request.image_id,
            disk_limit_bytes=context.request.disk_limit_bytes,
        )
        result.rootfs_result = rootfs_result
        if rootfs_result.status is ContainerRootfsStatus.Failed:
            # Falling through would hand the container the shared image directory
            # as its writable root, which is the isolation break this exists to
            # prevent. Fail the startup instead.
            raise RuntimeError(
                rootfs_result.reason or "failed to prepare the container root filesystem"
            )

    @staticmethod
    def _container_hostname(
        context: ContainerExecutionContext,
        result: ContainerExecutionResult,
    ) -> str:
        identity = result.network_result.identity if result.network_result is not None else None
        pod_address = (
            identity.pod_address
            if identity is not None and identity.pod_address
            else context.request.container_id
        )
        bind_port = result.bind_ports[0] if result.bind_ports else CONTAINER_INNER_PORT
        return f"{pod_address}:{bind_port}"

    def _publish_container_routes(
        self,
        context: ContainerExecutionContext,
        result: ContainerExecutionResult,
    ) -> None:
        if self.route_publisher is None:
            return
        self.route_publisher.publish_container_routes(
            context,
            port_bindings=result.port_bindings,
        )

    def _run_runtime(
        self,
        context: ContainerExecutionContext,
        result: ContainerExecutionResult,
        spec: OciRuntimeContainerSpec,
        on_started: Callable[[int], None],
        holder: dict[str, ContainerRuntimeRunResult],
    ) -> None:
        monitor = _RuntimeMonitorState()
        log_capture = (
            self.container_logs.begin(context.request) if self.container_logs is not None else None
        )
        output_sink = log_capture.process_output_sink if log_capture is not None else None

        def monitored_started(pid: int) -> None:
            monitor.start(self.runtime_monitor, context.request, pid)
            on_started(pid)

        try:
            restored = (
                self.checkpoint_restorer.restore(
                    context,
                    spec,
                    on_started=monitored_started,
                    output_sink=output_sink,
                )
                if context.checkpoint_id and self.checkpoint_restorer is not None
                else None
            )
            if (
                context.checkpoint_id
                and restored is None
                and context.startup_kind is WorkerStartupKind.Sandbox
            ):
                raise RuntimeError("checkpoint restore is not configured on this worker")
            holder["run_result"] = restored or self.runtime.run(
                spec,
                on_started=monitored_started,
                output_sink=output_sink,
            )
        except ContainerRuntimeStartError as exc:
            result.runtime_output = _redact_runtime_output(exc.output, context.request)
            raise
        finally:
            result.monitoring = monitor.stop()
            if log_capture is not None:
                result.container_logs = log_capture.close()
                if not result.container_logs.flushed:
                    detail = result.container_logs.last_error or (
                        f"{result.container_logs.pending_entries} entries remain unacknowledged"
                    )
                    result.lifecycle_errors.append(f"container log capture did not flush: {detail}")

    def _mark_running(
        self,
        result: ContainerExecutionResult,
        context: ContainerExecutionContext,
    ) -> bool:
        return self._phase(
            result,
            ContainerExecutionPhase.MarkRunning,
            lambda: self._update_running_status(context),
            skip=self.status_repository is None,
            request=context.request,
        )

    def _complete_checkpoint_startup(
        self,
        context: ContainerExecutionContext,
        result: ContainerExecutionResult,
    ) -> None:
        if self.automatic_checkpoints is None:
            raise RuntimeError("automatic checkpoint coordination is not configured")
        result.automatic_checkpoint_id = self.automatic_checkpoints.checkpoint_or_complete_restore(
            context,
            container_hostname=self._container_hostname(context, result),
        )

    def _prepare_sandbox_docker(self, context: ContainerExecutionContext) -> None:
        if self.sandbox_docker_preparer is None:
            msg = "Docker-enabled sandbox lifecycle is not configured"
            raise RuntimeError(msg)
        self.sandbox_docker_preparer.prepare(context.request.container_id)

    def _update_running_status(self, context: ContainerExecutionContext) -> None:
        if self.status_repository is None:
            return
        plan = self.status_repository.update_container_status(
            context.request.container_id,
            SchedulerContainerStatus.Running,
            ttl_seconds=DEFAULT_CONTAINER_STATE_TTL_SECONDS,
        )
        if plan.next_status is not SchedulerContainerStatus.Running:
            msg = (
                f"container {context.request.container_id} remained "
                f"{plan.next_status.value} when the worker marked it running"
            )
            raise RuntimeError(msg)

    def _prepare_runtime(
        self,
        context: ContainerExecutionContext,
        *,
        spec: OciRuntimeContainerSpec,
        mount_result: ContainerMountSetupResult,
        network_result: ContainerNetworkSetupResult | None,
        port_bindings: list[PortBinding],
    ) -> None:
        self.runtime.prepare(spec)
        if self.instance_recorder is None:
            return
        self.instance_recorder.record_container_instance(
            context,
            spec=spec,
            mount_result=mount_result,
            network_result=network_result,
            port_bindings=port_bindings,
        )

    def _handle_oom(
        self,
        result: ContainerExecutionResult,
        context: ContainerExecutionContext,
    ) -> None:
        if self.oom_supervisor is None or result.oom_watcher is None:
            return
        result.oom_result = self.oom_supervisor.handle_oom(context.request, result.oom_watcher)

    def _publish_exit_event(
        self,
        result: ContainerExecutionResult,
        context: ContainerExecutionContext,
        *,
        raw_exit_code: int,
        mapped_exit_code: int,
        stop_reason: StopContainerReason,
        oom_killed: bool,
    ) -> None:
        if self.exit_events is None:
            return
        exit_reason = container_exit_reason(
            mapped_exit_code,
            stop_reason=stop_reason,
            oom_killed=oom_killed,
        )
        payload = populate_container_event(
            ContainerEventPayload(
                id=CONTAINER_EXIT_EVENT_ID,
                reason=event_stop_reason(stop_reason),
                source="worker-runtime",
                message=CONTAINER_EXIT_MESSAGE,
                attrs={
                    "raw_exit_code": str(raw_exit_code),
                    "mapped_exit_code": str(mapped_exit_code),
                    "oom_killed": str(oom_killed).lower(),
                    "exit_reason": exit_reason,
                    "reason": stop_reason.value,
                    **_runtime_output_attrs(result.runtime_output),
                },
            ),
            context.request,
        )
        self.exit_events.publish_container_exit(payload)
        result.exit_event = payload

    def _finalize(
        self,
        result: ContainerExecutionResult,
        context: ContainerExecutionContext,
        *,
        exit_code: int,
        stop_reason: StopContainerReason,
        oom_killed: bool,
        failed_phase: ContainerExecutionPhase | None = None,
        failure_detail: str = "",
    ) -> None:
        self._phase(
            result,
            ContainerExecutionPhase.Finalize,
            lambda: self._set_finalization(
                result,
                context,
                exit_code=exit_code,
                stop_reason=stop_reason,
                oom_killed=oom_killed,
                failed_phase=failed_phase,
                failure_detail=failure_detail,
            ),
            request=context.request,
        )
        if result.finalization is not None and context.run_delayed_cleanup:
            self._phase(
                result,
                ContainerExecutionPhase.DelayedCleanup,
                lambda: self._set_delayed_cleanup(result),
                request=context.request,
            )

    def _finalize_startup_failure(
        self,
        result: ContainerExecutionResult,
        context: ContainerExecutionContext,
    ) -> None:
        """Finalize a container that never reached its run phase.

        The reason travels with the exit code because the asynchronous lifecycle
        event reporting it arrives after the task is already terminal.
        """
        failed_phase, detail = _first_phase_failure(result)
        safe_detail = _redact_runtime_output(detail, context.request) if detail else ""
        self._record_startup_diagnostic(context, failed_phase, safe_detail)
        self._finalize(
            result,
            context,
            exit_code=1,
            stop_reason=StopContainerReason.Unknown,
            oom_killed=False,
            failed_phase=failed_phase,
            failure_detail=safe_detail,
        )

    def _record_startup_diagnostic(
        self,
        context: ContainerExecutionContext,
        failed_phase: ContainerExecutionPhase | None,
        detail: str,
    ) -> None:
        """Put why a container never started into its own log stream.

        Log capture otherwise begins inside the run phase, so a container that dies
        before it has no logs at all and the owner sees an empty stream.
        """
        if self.container_logs is None or failed_phase is None:
            return
        message = f"container startup failed during {failed_phase.value}"
        if detail:
            message = f"{message}: {detail}"
        capture = self.container_logs.begin(context.request)
        try:
            capture.record_diagnostic(message)
        finally:
            capture.close()

    def _set_finalization(
        self,
        result: ContainerExecutionResult,
        context: ContainerExecutionContext,
        *,
        exit_code: int,
        stop_reason: StopContainerReason,
        oom_killed: bool,
        failed_phase: ContainerExecutionPhase | None = None,
        failure_detail: str = "",
    ) -> None:
        result.finalization = self.finalizer.finalize(
            ContainerFinalizationRequest(
                request=context.request,
                exit_code=exit_code,
                stop_reason=stop_reason,
                oom_killed=oom_killed,
                failed_phase=failed_phase,
                failure_detail=failure_detail,
            )
        )

    def _set_delayed_cleanup(self, result: ContainerExecutionResult) -> None:
        if result.finalization is None:
            return
        result.delayed_cleanup = self.finalizer.complete_delayed_cleanup(result.finalization.plan)

    def _phase[ActionResult](
        self,
        result: ContainerExecutionResult,
        phase: ContainerExecutionPhase,
        action: Callable[[], ActionResult],
        *,
        skip: bool = False,
        request: ContainerRequestContext | None = None,
    ) -> bool:
        if skip:
            result.phases.append(ContainerExecutionPhaseResult(phase=phase, skipped=True))
            return True
        started_at = datetime.now(UTC)
        try:
            action()
        except Exception as exc:  # pragma: no cover - defensive boundary capture
            self._publish_phase_lifecycle(
                result,
                phase,
                request=request,
                started_at=started_at,
                success=False,
                attrs={
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    **_runtime_output_attrs(result.runtime_output),
                },
            )
            result.phases.append(
                ContainerExecutionPhaseResult(
                    phase=phase,
                    ok=False,
                    error_message=f"{type(exc).__name__}: {exc}",
                )
            )
            return False
        self._publish_phase_lifecycle(
            result,
            phase,
            request=request,
            started_at=started_at,
            success=True,
        )
        result.phases.append(ContainerExecutionPhaseResult(phase=phase))
        return True

    def _publish_phase_lifecycle(
        self,
        result: ContainerExecutionResult,
        phase: ContainerExecutionPhase,
        *,
        request: ContainerRequestContext | None,
        started_at: datetime,
        success: bool,
        attrs: dict[str, str] | None = None,
    ) -> None:
        if self.lifecycle_events is None or request is None:
            return
        try:
            self.lifecycle_events.publish_container_lifecycle(
                container_lifecycle_from_duration(
                    phase.value,
                    request,
                    started_at=started_at,
                    duration=datetime.now(UTC) - started_at,
                    success=success,
                    attrs={"phase": phase.value, **(attrs or {})},
                )
            )
        except Exception as exc:  # pragma: no cover - event publishing must not fail runtime
            result.lifecycle_errors.append(f"{phase.value}: {type(exc).__name__}: {exc}")


@dataclass(slots=True)
class _RuntimeMonitorState:
    handle: ContainerRuntimeMonitorHandle | None = None

    def start(
        self,
        monitor: ContainerRuntimeMonitor | None,
        request: ContainerRequestContext,
        pid: int,
    ) -> None:
        if monitor is None or self.handle is not None:
            return
        self.handle = monitor.start_monitoring(request, started_pid=pid)

    def stop(self) -> ContainerRuntimeMonitoringResult | None:
        if self.handle is None:
            return None
        return self.handle.stop()


def _first_phase_failure(
    result: ContainerExecutionResult,
) -> tuple[ContainerExecutionPhase | None, str]:
    """Return the first phase that failed and its recorded error.

    Forward order so the root cause wins rather than a later re-raise wrapper.
    """
    for phase in result.phases:
        if phase.error_message:
            return phase.phase, phase.error_message
    return None, ""


def _runtime_output_attrs(output: str) -> dict[str, str]:
    if not output:
        return {}
    return {"runtime_output_tail": output[-4000:]}


def _redact_runtime_output(output: str, request: ContainerRequestContext) -> str:
    sensitive_values: set[str] = set()
    sensitive_keys = set(request.secret_names)
    sensitive_keys.add(GATEWAY_TOKEN_ENV)
    for item in request.env:
        key, separator, value = item.partition("=")
        if separator and value and (key in sensitive_keys or _sensitive_environment_key(key)):
            sensitive_values.add(value)
    workspace_storage = request.workspace_storage_credentials
    if workspace_storage is not None:
        sensitive_values.update({workspace_storage.access_key, workspace_storage.secret_key})
    for mount in request.mounts:
        mountpoint = mount.mountpoint_config
        if mountpoint is not None:
            sensitive_values.update({mountpoint.access_key, mountpoint.secret_key})
    redacted = output
    for value in sorted(sensitive_values, key=len, reverse=True):
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted[-4000:]


def _sensitive_environment_key(key: str) -> bool:
    normalized = key.upper()
    return any(
        marker in normalized
        for marker in (
            "ACCESS_KEY",
            "API_KEY",
            "CREDENTIAL",
            "PASSWORD",
            "PRIVATE_KEY",
            "SECRET",
            "TOKEN",
        )
    )
