from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from time import sleep

from foundation.process import (
    ProcessOutputChunk,
    ProcessOutputSink,
    ProcessOutputStream,
)
from pydantic import JsonValue
from scheduler.state import (
    DEFAULT_CONTAINER_STATE_TTL_SECONDS,
    ContainerStatusUpdatePlan,
    SchedulerContainerStatus,
)
from shared.container_requests import StopContainerReason, WorkerStartupKind
from shared.scheduling import SchedulerContainerState
from shared.worker_events import WorkerEventRecord
from storage_client.mounts import StorageMountResult
from worker.container_execution import (
    ContainerExecutionContext,
    ContainerExecutionPhase,
    ContainerImageLoadResult,
    ContainerMountSetupResult,
    ContainerNetworkSetupResult,
    ContainerRuntimeRunResult,
    ContainerRuntimeStartError,
    WorkerContainerExecutionService,
)
from worker.container_logs import WorkerContainerLogCaptureService
from worker.container_rootfs import (
    ContainerRootfsReleaseResult,
    ContainerRootfsSetupResult,
    ContainerRootfsStatus,
)
from worker.events import (
    ContainerEventPayload,
    ContainerExitCode,
    ContainerLifecyclePayload,
    ContainerRequestContext,
)
from worker.execution import ContainerNetworkIdentity, OciLinuxResources, PortBinding
from worker.finalization import (
    CONTAINER_STATE_TTL_WHILE_PENDING_SECONDS,
    ContainerFinalizationStep,
    WorkerContainerFinalizationService,
)
from worker.gpu import ContainerGpuAssignmentResult
from worker.monitoring import (
    ContainerRuntimeMonitor,
    ContainerRuntimeMonitorSettings,
    WorkerContainerRuntimeMonitor,
)
from worker.oci_spec import OciRuntimeContainerSpec
from worker.repository_payloads import (
    AppendContainerLogsResponse,
    ContainerLogBatchEntry,
    ContainerLogEntryKind,
)
from worker.runtime_config import OciRuntimeName, RuntimeBinaryConfig
from worker.supervision import WorkerEventSink, WorkerSupervisionService


@dataclass(slots=True)
class CallLog:
    calls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AddressPublisher:
    log: CallLog

    def publish_worker_address(self, request: ContainerRequestContext) -> None:
        self.log.calls.append(f"address:{request.container_id}")


@dataclass(slots=True)
class ImageLoader:
    log: CallLog
    result: ContainerImageLoadResult = field(default_factory=ContainerImageLoadResult)
    env_seen: list[str] = field(default_factory=list)

    def load_image(self, request: ContainerRequestContext) -> ContainerImageLoadResult:
        self.log.calls.append(f"image:{request.container_id}")
        self.env_seen = list(request.env)
        return self.result


@dataclass(slots=True)
class PortAllocator:
    log: CallLog

    def allocate_ports(self, count: int) -> list[int]:
        self.log.calls.append(f"ports:{count}")
        return [30_000 + index for index in range(1, count + 1)]


@dataclass(slots=True)
class RoutePublisher:
    log: CallLog
    bindings: list[PortBinding] = field(default_factory=list)

    def publish_container_routes(
        self,
        context: ContainerExecutionContext,
        *,
        port_bindings: list[PortBinding],
    ) -> None:
        self.log.calls.append(f"routes:{context.request.container_id}:{len(port_bindings)}")
        self.bindings = list(port_bindings)


@dataclass(slots=True)
class NetworkPreparer:
    log: CallLog
    identity: ContainerNetworkIdentity | None = None

    def setup_network(
        self,
        context: ContainerExecutionContext,
        *,
        port_bindings: list[PortBinding],
    ) -> ContainerNetworkSetupResult:
        self.log.calls.append(f"network:{context.request.container_id}:{len(port_bindings)}")
        return ContainerNetworkSetupResult(
            enabled=True,
            identity=self.identity
            or ContainerNetworkIdentity(container_id=context.request.container_id),
            reason="network prepared",
        )


@dataclass(slots=True)
class AutomaticCheckpointCoordinator:
    hostnames: list[str] = field(default_factory=list)
    log: CallLog | None = None
    fail: bool = False

    def prepare_mount(
        self,
        context: ContainerExecutionContext,
        mount_result: ContainerMountSetupResult,
    ) -> ContainerMountSetupResult:
        _ = context
        return mount_result

    def checkpoint_or_complete_restore(
        self,
        context: ContainerExecutionContext,
        *,
        container_hostname: str,
    ) -> str:
        _ = context
        if self.log is not None:
            self.log.calls.append("checkpoint-startup")
        self.hostnames.append(container_hostname)
        if self.fail:
            raise RuntimeError("checkpoint startup failed")
        return "checkpoint-1"

    def cleanup(self, container_id: str) -> None:
        _ = container_id


@dataclass(slots=True)
class FallbackCheckpointRestorer:
    calls: list[str] = field(default_factory=list)
    output_sink_received: bool = False

    def restore(
        self,
        context: ContainerExecutionContext,
        spec: OciRuntimeContainerSpec,
        *,
        on_started: Callable[[int], None],
        output_sink: ProcessOutputSink | None = None,
    ) -> ContainerRuntimeRunResult | None:
        _ = (spec, on_started)
        self.calls.append(context.request.container_id)
        self.output_sink_received = output_sink is not None
        return None


@dataclass(slots=True)
class MountPreparer:
    log: CallLog
    fail: bool = False

    def setup_mounts(self, request: ContainerRequestContext) -> ContainerMountSetupResult:
        self.log.calls.append(f"mounts:{request.container_id}")
        if self.fail:
            raise RuntimeError("mount setup failed")
        return ContainerMountSetupResult(reason="test mounts prepared")


@dataclass(slots=True)
class WorkspaceStorageMounter:
    log: CallLog
    fail: bool = False

    def ensure_workspace_storage(self, request: ContainerRequestContext) -> None:
        self.log.calls.append(f"workspace-storage:{request.container_id}")
        if self.fail:
            raise RuntimeError("workspace storage mount failed")

    def cleanup_unused(
        self,
        *,
        active_workspace_names: set[str],
    ) -> list[StorageMountResult]:
        _ = active_workspace_names
        return []


@dataclass(slots=True)
class RootfsPreparer:
    log: CallLog
    released: list[str] = field(default_factory=list)
    disk_limits: list[int] = field(default_factory=list)

    def prepare(
        self,
        *,
        container_id: str,
        image_id: str,
        disk_limit_bytes: int = 0,
    ) -> ContainerRootfsSetupResult:
        _ = image_id
        self.disk_limits.append(disk_limit_bytes)
        self.log.calls.append(f"rootfs:{container_id}")
        return ContainerRootfsSetupResult(
            container_id=container_id,
            status=ContainerRootfsStatus.Mounted,
            root_path=f"/var/lib/lazycloud/container-rootfs/{container_id}/merged",
            upper_path=f"/var/lib/lazycloud/container-rootfs/{container_id}/upper",
        )

    def release(self, container_id: str) -> ContainerRootfsReleaseResult:
        self.released.append(container_id)
        return ContainerRootfsReleaseResult(
            container_id=container_id,
            unmounted=True,
            removed=True,
        )


@dataclass(slots=True)
class SpecBuilder:
    log: CallLog
    gpu_result: ContainerGpuAssignmentResult | None = None
    rootfs_result: ContainerRootfsSetupResult | None = None

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
    ) -> OciRuntimeContainerSpec:
        self.log.calls.append(f"spec:{context.request.container_id}")
        self.rootfs_result = rootfs_result
        self.gpu_result = gpu_result
        container_id = context.request.container_id
        bundle_path = f"/tmp/{container_id}"
        runtime_ports: list[JsonValue] = list(bind_ports)
        runtime_bindings: list[JsonValue] = [item.model_dump(mode="json") for item in port_bindings]
        runtime_spec: dict[str, JsonValue] = {
            "bind_ports": runtime_ports,
            "port_bindings": runtime_bindings,
            "mount_count": len(mount_result.oci_mounts),
            "network_enabled": (network_result.enabled if network_result is not None else False),
        }
        return OciRuntimeContainerSpec(
            container_id=container_id,
            runtime=RuntimeBinaryConfig(runtime=context.runtime),
            bundle_path=bundle_path,
            config_path=f"{bundle_path}/config.json",
            process_spec_dir=f"{bundle_path}/processes",
            spec=runtime_spec,
        )


@dataclass(slots=True)
class RuntimeExecutor:
    log: CallLog
    result: ContainerRuntimeRunResult = field(
        default_factory=lambda: ContainerRuntimeRunResult(exit_code=0)
    )
    failure: ContainerRuntimeStartError | None = None
    output_sink_received: bool = False

    def prepare(self, spec: OciRuntimeContainerSpec) -> None:
        _ = spec
        self.log.calls.append("runtime-prepare")

    def run(
        self,
        spec: OciRuntimeContainerSpec,
        *,
        on_started: Callable[[int], None],
        output_sink: ProcessOutputSink | None = None,
    ) -> ContainerRuntimeRunResult:
        _ = spec
        self.log.calls.append("runtime-run")
        self.output_sink_received = output_sink is not None
        if output_sink is not None:
            output_sink(
                ProcessOutputChunk(
                    stream=ProcessOutputStream.Stdout,
                    text="checkpoint-e2e-continuity\n",
                )
            )
        on_started(self.result.started_pid or 123)
        if self.failure is not None:
            raise self.failure
        return self.result


@dataclass(slots=True)
class SandboxDockerPreparer:
    log: CallLog
    fail: bool = False

    def prepare(self, container_id: str) -> None:
        self.log.calls.append(f"sandbox-docker:{container_id}")
        if self.fail:
            raise RuntimeError("Docker daemon failed to start")


@dataclass(slots=True)
class ContainerLogSink:
    batches: list[list[ContainerLogBatchEntry]] = field(default_factory=list)

    def publish_container_logs(
        self,
        *,
        container_id: str,
        capture_id: str,
        entries: list[ContainerLogBatchEntry],
    ) -> AppendContainerLogsResponse:
        assert container_id == "ctr-1"
        assert capture_id
        self.batches.append(list(entries))
        return AppendContainerLogsResponse(
            accepted_through=entries[-1].sequence,
            appended_count=len(entries),
        )


@dataclass(slots=True)
class ExitEvents:
    payloads: list[ContainerEventPayload] = field(default_factory=list)

    def publish_container_exit(self, payload: ContainerEventPayload) -> None:
        self.payloads.append(payload)


@dataclass(slots=True)
class GpuAssigner:
    assignments: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    fail: bool = False

    def assign_gpus(self, request: ContainerRequestContext) -> ContainerGpuAssignmentResult:
        self.assignments.append(request.container_id)
        if self.fail:
            return ContainerGpuAssignmentResult(
                container_id=request.container_id,
                requested_count=request.gpu_count,
                ok=False,
                error_message="gpu unavailable",
            )
        return ContainerGpuAssignmentResult(
            container_id=request.container_id,
            requested_count=request.gpu_count,
            assigned_devices=[0],
            cdi_devices=["nvidia.com/gpu=0"],
            reason="assigned",
        )

    def release_gpu(self, container_id: str) -> None:
        self.released.append(container_id)


@dataclass(slots=True)
class LifecycleEvents:
    payloads: list[ContainerLifecyclePayload] = field(default_factory=list)
    fail: bool = False

    def publish_container_lifecycle(self, payload: ContainerLifecyclePayload) -> None:
        if self.fail:
            raise RuntimeError("lifecycle sink unavailable")
        self.payloads.append(payload)


@dataclass(slots=True)
class CredentialHydrator:
    log: CallLog

    def hydrate_container_credentials(
        self,
        context: ContainerExecutionContext,
    ) -> ContainerExecutionContext:
        self.log.calls.append(f"credentials:{context.request.container_id}")
        return context.model_copy(
            update={
                "request": context.request.model_copy(
                    update={"env": [*context.request.env, "TOKEN=hydrated"]}
                )
            }
        )


@dataclass(slots=True)
class FinalizationRepository:
    exit_codes: list[tuple[str, int, StopContainerReason]] = field(default_factory=list)
    failure_details: list[tuple[ContainerExecutionPhase | None, str]] = field(default_factory=list)
    status_updates: list[tuple[str, SchedulerContainerStatus, int]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    running_error: RuntimeError | None = None
    running_result_status: SchedulerContainerStatus = SchedulerContainerStatus.Running

    def get_container_state(self, container_id: str) -> SchedulerContainerState:
        return SchedulerContainerState(
            container_id=container_id,
            stub_id="stub-1",
            workspace_id="workspace-1",
            status=(
                self.status_updates[-1][1]
                if self.status_updates
                else SchedulerContainerStatus.Pending
            ),
        )

    def set_exit_code(
        self,
        container_id: str,
        exit_code: int,
        *,
        exited_at: datetime,
        termination_reason: StopContainerReason,
        failed_phase: ContainerExecutionPhase | None = None,
        failure_detail: str = "",
    ) -> None:
        self.exit_codes.append((container_id, exit_code, termination_reason))
        self.failure_details.append((failed_phase, failure_detail))

    def update_container_status(
        self,
        container_id: str,
        status: SchedulerContainerStatus,
        *,
        ttl_seconds: int,
    ) -> ContainerStatusUpdatePlan:
        if status is SchedulerContainerStatus.Running and self.running_error is not None:
            raise self.running_error
        next_status = (
            self.running_result_status if status is SchedulerContainerStatus.Running else status
        )
        self.status_updates.append((container_id, next_status, ttl_seconds))
        return ContainerStatusUpdatePlan(
            container_id=container_id,
            previous_status=SchedulerContainerStatus.Pending,
            next_status=next_status,
            changed=next_status is not SchedulerContainerStatus.Pending,
            ttl_seconds=ttl_seconds,
        )

    def delete_container_state(self, container_id: str, *, storage_released: bool = False) -> bool:
        self.deleted.append(container_id)
        return True


@dataclass(slots=True)
class Cleanup:
    calls: list[ContainerFinalizationStep] = field(default_factory=list)

    def release_gpu(self, container_id: str) -> None:
        _ = container_id
        self.calls.append(ContainerFinalizationStep.ReleaseGpu)

    def teardown_network(self, container_id: str) -> None:
        _ = container_id
        self.calls.append(ContainerFinalizationStep.TeardownNetwork)

    def remove_uploads(self, container_id: str) -> None:
        _ = container_id
        self.calls.append(ContainerFinalizationStep.RemoveUploads)

    def remove_source_workspace(self, container_id: str) -> None:
        _ = container_id
        self.calls.append(ContainerFinalizationStep.RemoveSourceWorkspace)

    def force_stop_if_running(self, container_id: str) -> None:
        _ = container_id
        self.calls.append(ContainerFinalizationStep.ForceKillIfRunning)

    def stop_oom_watcher(self, container_id: str) -> None:
        _ = container_id
        self.calls.append(ContainerFinalizationStep.StopOomWatcher)

    def unmount_request_mounts(self, container_id: str) -> None:
        _ = container_id
        self.calls.append(ContainerFinalizationStep.UnmountRequestMounts)

    def release_container_rootfs(self, container_id: str) -> None:
        _ = container_id
        self.calls.append(ContainerFinalizationStep.ReleaseContainerRootfs)

    def delete_local_state(self, container_id: str) -> None:
        _ = container_id
        self.calls.append(ContainerFinalizationStep.DeleteLocalState)


@dataclass(slots=True)
class EventSink(WorkerEventSink):
    records: list[WorkerEventRecord] = field(default_factory=list)

    def append(self, record: WorkerEventRecord) -> WorkerEventRecord:
        self.records.append(record)
        return record


@dataclass(slots=True)
class Stopper:
    def prepare_runtime_resources(self, container_id: str, resources: OciLinuxResources) -> None:
        pass

    stopped: list[tuple[str, bool]] = field(default_factory=list)

    def stop_container(
        self,
        container_id: str,
        *,
        force: bool,
        reason: StopContainerReason = StopContainerReason.Unknown,
    ) -> None:
        self.stopped.append((container_id, force))


def test_worker_container_execution_service_runs_full_lifecycle() -> None:
    log = CallLog()
    repo = FinalizationRepository()
    cleanup = Cleanup()
    exit_events = ExitEvents()
    service = _service(log, repo=repo, cleanup=cleanup, exit_events=exit_events)

    result = service.execute(
        ContainerExecutionContext(
            request=_request(),
            startup_kind=WorkerStartupKind.Function,
            ports=[8080],
            runtime=OciRuntimeName.Runc,
            cgroup_path="/sys/fs/cgroup/ctr-1",
        )
    )

    assert result.ok
    assert [phase.phase for phase in result.phases] == [
        ContainerExecutionPhase.PublishWorkerAddress,
        ContainerExecutionPhase.LoadImage,
        ContainerExecutionPhase.AllocatePorts,
        ContainerExecutionPhase.SetupNetwork,
        ContainerExecutionPhase.SetupWorkspaceStorage,
        ContainerExecutionPhase.SetupMounts,
        ContainerExecutionPhase.PrepareRootfs,
        ContainerExecutionPhase.AssignGpu,
        ContainerExecutionPhase.BuildSpec,
        ContainerExecutionPhase.PrepareRuntime,
        ContainerExecutionPhase.PrepareSandboxDocker,
        ContainerExecutionPhase.CompleteCheckpointStartup,
        ContainerExecutionPhase.MarkRunning,
        ContainerExecutionPhase.PublishContainerRoutes,
        ContainerExecutionPhase.RunRuntime,
        ContainerExecutionPhase.PublishExitEvent,
        ContainerExecutionPhase.Finalize,
    ]
    assert log.calls == [
        "address:ctr-1",
        "image:ctr-1",
        "ports:2",
        "mounts:ctr-1",
        "rootfs:ctr-1",
        "spec:ctr-1",
        "runtime-prepare",
        "runtime-run",
    ]
    assert [(item.host_port, item.container_port) for item in result.port_bindings] == [
        (30_001, 8080),
        (30_002, 2222),
    ]
    assert repo.exit_codes == [("ctr-1", 0, StopContainerReason.Unknown)]
    assert repo.status_updates == [
        ("ctr-1", SchedulerContainerStatus.Running, DEFAULT_CONTAINER_STATE_TTL_SECONDS),
        (
            "ctr-1",
            SchedulerContainerStatus.Stopping,
            CONTAINER_STATE_TTL_WHILE_PENDING_SECONDS,
        ),
    ]
    assert exit_events.payloads[0].attrs["raw_exit_code"] == "0"
    assert exit_events.payloads[0].attrs["mapped_exit_code"] == "0"
    assert result.oom_watcher is not None
    assert result.oom_watcher.cgroup_path == "/sys/fs/cgroup/ctr-1"
    assert cleanup.calls == [
        ContainerFinalizationStep.TeardownNetwork,
        ContainerFinalizationStep.RemoveUploads,
        ContainerFinalizationStep.RemoveSourceWorkspace,
    ]
    route_phase = next(
        phase
        for phase in result.phases
        if phase.phase is ContainerExecutionPhase.PublishContainerRoutes
    )
    assert route_phase.skipped
    storage_phase = next(
        phase
        for phase in result.phases
        if phase.phase is ContainerExecutionPhase.SetupWorkspaceStorage
    )
    assert storage_phase.skipped


def test_worker_container_execution_fails_before_running_when_docker_startup_fails() -> None:
    log = CallLog()
    repo = FinalizationRepository()
    service = _service(
        log,
        repo=repo,
        sandbox_docker_preparer=SandboxDockerPreparer(log, fail=True),
    )

    result = service.execute(
        ContainerExecutionContext(
            request=_request(),
            startup_kind=WorkerStartupKind.Sandbox,
            docker_enabled=True,
        )
    )

    assert not result.ok
    assert result.failed_phase is ContainerExecutionPhase.PrepareSandboxDocker
    assert all(
        status is not SchedulerContainerStatus.Running for _, status, _ in repo.status_updates
    )


def test_failed_checkpoint_startup_is_not_promoted_to_running_by_the_monitor() -> None:
    class SlowFailedCheckpointStartup(AutomaticCheckpointCoordinator):
        def checkpoint_or_complete_restore(
            self, context: ContainerExecutionContext, *, container_hostname: str
        ) -> str:
            sleep(0.05)
            raise RuntimeError("checkpoint startup failed")

    log = CallLog()
    repo = FinalizationRepository()
    checkpoints = SlowFailedCheckpointStartup()
    routes = RoutePublisher(log)
    monitor = WorkerContainerRuntimeMonitor(
        container_states=repo,
        settings=ContainerRuntimeMonitorSettings(sample_interval_seconds=0.001),
    )
    service = _service(
        log,
        repo=repo,
        automatic_checkpoints=checkpoints,
        route_publisher=routes,
        runtime_monitor=monitor,
    )

    result = service.execute(
        ContainerExecutionContext(
            request=_request(),
            startup_kind=WorkerStartupKind.Endpoint,
            ports=[8001],
            checkpoint_enabled=True,
        )
    )

    assert not result.ok
    assert result.failed_phase is ContainerExecutionPhase.CompleteCheckpointStartup
    assert not any(
        status is SchedulerContainerStatus.Running for _, status, _ in repo.status_updates
    )
    assert "routes:ctr-1:2" not in log.calls
    assert result.monitoring is not None
    assert result.monitoring.started_pid == 0


def test_deployment_restore_fallback_starts_fresh_and_completes_handshake() -> None:
    log = CallLog()
    checkpoints = AutomaticCheckpointCoordinator()
    restorer = FallbackCheckpointRestorer()
    service = _service(
        log,
        checkpoint_restorer=restorer,
        automatic_checkpoints=checkpoints,
    )

    result = service.execute(
        ContainerExecutionContext(
            request=_request(),
            startup_kind=WorkerStartupKind.Endpoint,
            checkpoint_id="checkpoint-bad",
            ports=[8001],
        )
    )

    assert result.ok
    assert restorer.calls == ["ctr-1"]
    assert "runtime-run" in log.calls
    assert result.automatic_checkpoint_id == "checkpoint-1"
    assert checkpoints.hostnames == ["ctr-1:30001"]


def test_restore_fallback_and_fresh_run_share_one_log_capture_and_flush() -> None:
    log = CallLog()
    restorer = FallbackCheckpointRestorer()
    sink = ContainerLogSink()
    service = _service(
        log,
        checkpoint_restorer=restorer,
        automatic_checkpoints=AutomaticCheckpointCoordinator(),
    )
    service.container_logs = WorkerContainerLogCaptureService(sink=sink)

    result = service.execute(
        ContainerExecutionContext(
            request=_request(),
            startup_kind=WorkerStartupKind.Endpoint,
            checkpoint_id="checkpoint-bad",
            ports=[8001],
        )
    )

    assert result.ok
    assert restorer.output_sink_received
    assert isinstance(service.runtime, RuntimeExecutor)
    assert service.runtime.output_sink_received
    assert result.container_logs is not None
    assert result.container_logs.flushed
    entries = [entry for batch in sink.batches for entry in batch]
    assert [entry.sequence for entry in entries] == list(range(len(entries)))
    assert entries[0].message == "checkpoint-e2e-continuity"
    assert entries[-1].kind is ContainerLogEntryKind.Flush


def test_worker_container_execution_cleans_runtime_when_cancelled_after_start() -> None:
    log = CallLog()
    repo = FinalizationRepository(running_result_status=SchedulerContainerStatus.Stopping)
    cleanup = Cleanup()
    service = _service(log, repo=repo, cleanup=cleanup)

    result = service.execute(
        ContainerExecutionContext(
            request=_request(),
            startup_kind=WorkerStartupKind.Sandbox,
            run_delayed_cleanup=True,
        )
    )

    assert not result.ok
    assert result.failed_phase is ContainerExecutionPhase.MarkRunning
    failed_phases = [phase for phase in result.phases if not phase.ok]
    assert [phase.phase for phase in failed_phases] == [
        ContainerExecutionPhase.MarkRunning,
        ContainerExecutionPhase.RunRuntime,
    ]
    assert "remained stopping" in failed_phases[0].error_message
    assert "remained stopping" in failed_phases[1].error_message
    assert result.finalization is not None
    assert result.delayed_cleanup is not None
    assert repo.status_updates == [
        (
            "ctr-1",
            SchedulerContainerStatus.Stopping,
            DEFAULT_CONTAINER_STATE_TTL_SECONDS,
        ),
        (
            "ctr-1",
            SchedulerContainerStatus.Stopping,
            CONTAINER_STATE_TTL_WHILE_PENDING_SECONDS,
        ),
    ]
    assert repo.deleted == ["ctr-1"]
    assert cleanup.calls == [
        ContainerFinalizationStep.TeardownNetwork,
        ContainerFinalizationStep.RemoveUploads,
        ContainerFinalizationStep.RemoveSourceWorkspace,
        ContainerFinalizationStep.ForceKillIfRunning,
        ContainerFinalizationStep.StopOomWatcher,
        ContainerFinalizationStep.UnmountRequestMounts,
        ContainerFinalizationStep.ReleaseContainerRootfs,
        ContainerFinalizationStep.DeleteLocalState,
    ]


def test_worker_container_execution_service_handles_image_short_circuit() -> None:
    log = CallLog()
    repo = FinalizationRepository()
    service = _service(
        log,
        repo=repo,
        image_result=ContainerImageLoadResult(loaded=True, short_circuit_exit_code=0),
    )

    result = service.execute(ContainerExecutionContext(request=_request()))

    assert result.ok
    assert [phase.phase for phase in result.phases] == [
        ContainerExecutionPhase.PublishWorkerAddress,
        ContainerExecutionPhase.LoadImage,
        ContainerExecutionPhase.Finalize,
    ]
    assert log.calls == ["address:ctr-1", "image:ctr-1"]
    assert repo.exit_codes == [("ctr-1", 0, StopContainerReason.Unknown)]


def test_worker_container_execution_service_handles_oom_before_finalization() -> None:
    log = CallLog()
    repo = FinalizationRepository()
    cleanup = Cleanup()
    sink = EventSink()
    stopper = Stopper()
    service = _service(
        log,
        repo=repo,
        cleanup=cleanup,
        runtime_result=ContainerRuntimeRunResult(
            exit_code=0,
            oom_killed=True,
            started_pid=123,
        ),
        oom_supervisor=WorkerSupervisionService(
            worker_id="worker-1",
            event_sink=sink,
            container_stopper=stopper,
        ),
    )

    result = service.execute(
        ContainerExecutionContext(
            request=_request(gpu="L4", gpu_count=1),
            runtime=OciRuntimeName.Runsc,
            memory_limit_bytes=512 * 1024 * 1024,
        )
    )

    assert result.ok
    assert result.oom_result is not None
    assert result.oom_result.stop_invoked
    assert stopper.stopped == [("ctr-1", True)]
    assert repo.exit_codes == [
        ("ctr-1", int(ContainerExitCode.OomKill), StopContainerReason.Unknown)
    ]
    assert cleanup.calls[:4] == [
        ContainerFinalizationStep.ReleaseGpu,
        ContainerFinalizationStep.TeardownNetwork,
        ContainerFinalizationStep.RemoveUploads,
        ContainerFinalizationStep.RemoveSourceWorkspace,
    ]


def test_worker_container_execution_service_assigns_gpus_before_building_spec() -> None:
    log = CallLog()
    gpu = GpuAssigner()
    spec_builder = SpecBuilder(log)
    service = _service(
        log,
        spec_builder=spec_builder,
        gpu_assigner=gpu,
    )

    result = service.execute(
        ContainerExecutionContext(
            request=_request(gpu="L4", gpu_count=1),
            startup_kind=WorkerStartupKind.Function,
        )
    )

    assert result.ok
    assert gpu.assignments == ["ctr-1"]
    assert result.gpu_result is not None
    assert result.gpu_result.assigned_devices == [0]
    assert spec_builder.gpu_result is result.gpu_result
    assert log.calls.index("mounts:ctr-1") < log.calls.index("spec:ctr-1")


def test_worker_container_execution_service_stops_on_gpu_assignment_failure() -> None:
    log = CallLog()
    service = _service(log, gpu_assigner=GpuAssigner(fail=True))

    result = service.execute(ContainerExecutionContext(request=_request(gpu="L4", gpu_count=1)))

    assert not result.ok
    assert result.failed_phase is ContainerExecutionPhase.AssignGpu
    assert result.finalization is not None
    assert result.finalization.ok


def test_worker_container_execution_redacts_runtime_start_failure_output() -> None:
    log = CallLog()
    lifecycle = LifecycleEvents()
    secret = "runtime-secret-value"
    service = _service(
        log,
        lifecycle_events=lifecycle,
        runtime_failure=ContainerRuntimeStartError(
            "ctr-1",
            exit_code=1,
            output=f"runner failed with TOKEN={secret}\nimport failure",
        ),
    )

    result = service.execute(
        ContainerExecutionContext(
            request=_request().model_copy(update={"env": [f"GATEWAY_TOKEN={secret}"]}),
            startup_kind=WorkerStartupKind.Endpoint,
        )
    )

    assert not result.ok
    assert result.failed_phase is ContainerExecutionPhase.RunRuntime
    assert secret not in result.runtime_output
    assert "TOKEN=<redacted>" in result.runtime_output
    failed = next(
        payload
        for payload in lifecycle.payloads
        if payload.id == ContainerExecutionPhase.RunRuntime.value and not payload.success
    )
    assert failed.attrs["runtime_output_tail"] == result.runtime_output
    assert secret not in failed.model_dump_json()


def test_worker_container_execution_service_keeps_running_when_lifecycle_sink_fails() -> None:
    log = CallLog()
    service = _service(log, lifecycle_events=LifecycleEvents(fail=True))

    result = service.execute(ContainerExecutionContext(request=_request()))

    assert result.ok
    assert result.lifecycle_errors
    assert result.lifecycle_errors[0].startswith("publish-worker-address: RuntimeError")


def test_worker_container_execution_service_stops_on_mount_failure() -> None:
    log = CallLog()
    service = _service(log, mount_preparer=MountPreparer(log, fail=True))

    result = service.execute(ContainerExecutionContext(request=_request()))

    assert not result.ok
    assert result.failed_phase is ContainerExecutionPhase.SetupMounts
    assert result.finalization is not None
    assert result.finalization.ok
    assert log.calls == ["address:ctr-1", "image:ctr-1", "ports:2", "mounts:ctr-1"]


def _service(
    log: CallLog,
    *,
    repo: FinalizationRepository | None = None,
    cleanup: Cleanup | None = None,
    image_result: ContainerImageLoadResult | None = None,
    runtime_result: ContainerRuntimeRunResult | None = None,
    runtime_failure: ContainerRuntimeStartError | None = None,
    mount_preparer: MountPreparer | None = None,
    rootfs_preparer: RootfsPreparer | None = None,
    oom_supervisor: WorkerSupervisionService | None = None,
    exit_events: ExitEvents | None = None,
    route_publisher: RoutePublisher | None = None,
    network_preparer: NetworkPreparer | None = None,
    lifecycle_events: LifecycleEvents | None = None,
    runtime_monitor: ContainerRuntimeMonitor | None = None,
    gpu_assigner: GpuAssigner | None = None,
    spec_builder: SpecBuilder | None = None,
    image_loader: ImageLoader | None = None,
    credential_hydrator: CredentialHydrator | None = None,
    workspace_storage_mounter: WorkspaceStorageMounter | None = None,
    sandbox_docker_preparer: SandboxDockerPreparer | None = None,
    checkpoint_restorer: FallbackCheckpointRestorer | None = None,
    automatic_checkpoints: AutomaticCheckpointCoordinator | None = None,
) -> WorkerContainerExecutionService:
    final_repo = repo or FinalizationRepository()
    final_cleanup = cleanup or Cleanup()
    return WorkerContainerExecutionService(
        runtime_resources=Stopper(),
        address_publisher=AddressPublisher(log),
        image_loader=image_loader or ImageLoader(log, image_result or ContainerImageLoadResult()),
        port_allocator=PortAllocator(log),
        mount_preparer=mount_preparer or MountPreparer(log),
        rootfs_preparer=rootfs_preparer or RootfsPreparer(log),
        spec_builder=spec_builder or SpecBuilder(log),
        runtime=RuntimeExecutor(
            log,
            runtime_result or ContainerRuntimeRunResult(exit_code=0),
            runtime_failure,
        ),
        finalizer=WorkerContainerFinalizationService(final_repo, final_cleanup),
        route_publisher=route_publisher,
        network_preparer=network_preparer,
        workspace_storage_mounter=workspace_storage_mounter,
        gpu_assigner=gpu_assigner,
        sandbox_docker_preparer=sandbox_docker_preparer,
        credential_hydrator=credential_hydrator,
        oom_supervisor=oom_supervisor,
        exit_events=exit_events,
        lifecycle_events=lifecycle_events,
        runtime_monitor=runtime_monitor,
        status_repository=final_repo,
        checkpoint_restorer=checkpoint_restorer,
        automatic_checkpoints=automatic_checkpoints,
    )


def _request(
    *,
    gpu: str = "",
    gpu_count: int = 0,
) -> ContainerRequestContext:
    return ContainerRequestContext(
        container_id="ctr-1",
        workspace_id="workspace-1",
        stub_id="stub-1",
        app_id="app-1",
        env=["TASK_ID=task-1"],
        cpu_millicores=1000,
        memory_mib=512,
        gpu=gpu,
        gpu_count=gpu_count,
    )
