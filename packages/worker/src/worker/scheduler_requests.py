from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from shared.container_requests import (
    StopContainerReason,
    WorkerContainerRequestPayload,
    WorkerStartupKind,
)
from shared.contracts import ContractModel
from shared.image_building.authoring import LinuxArchitecture
from shared.scheduling import (
    DEFAULT_CONTAINER_STATE_TTL_SECONDS,
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRequest,
    WorkerCapacityChange,
    WorkerCapacityPlan,
    gpu_count_for_capacity,
)

from worker.container_execution import (
    ContainerExecutionContext,
    ContainerExecutionResult,
)
from worker.events import ContainerRequestContext
from worker.finalization import ContainerFinalizationRepository
from worker.image_build_execution import (
    WorkerImageBuildExecutionResult,
    is_image_build_scheduler_request,
)
from worker.status import (
    WorkerCancelledRequestAction,
    WorkerCancelledRequestPlan,
    plan_worker_cancelled_request,
)

MIB = 1024 * 1024


class WorkerSchedulerRequestAction(StrEnum):
    Idle = "idle"
    DropMissingState = "drop-missing-state"
    DropStoppingState = "drop-stopping-state"
    Execute = "execute"


class WorkerSchedulerRequestStatus(StrEnum):
    Idle = "idle"
    Dropped = "dropped"
    Executed = "executed"
    Error = "error"


class WorkerSchedulerRequestWorkerRepository(Protocol):
    def get_next_container_request(self, worker_id: str) -> SchedulerWorkerRequest | None: ...

    def update_worker_capacity(
        self,
        worker_id: str,
        request: SchedulerWorkerRequest,
        change: WorkerCapacityChange,
    ) -> WorkerCapacityPlan: ...


class WorkerSchedulerRequestContainerRepository(ContainerFinalizationRepository, Protocol):
    def get_container_state(self, container_id: str) -> SchedulerContainerState | None: ...


class WorkerSchedulerRequestLifecycle(Protocol):
    def register_container(
        self,
        request: ContainerRequestContext,
    ) -> None: ...

    def unregister_container(self, container_id: str) -> None: ...


class WorkerSchedulerRequestImageBuildExecutor(Protocol):
    def execute(self, request: SchedulerWorkerRequest) -> WorkerImageBuildExecutionResult: ...


class WorkerSchedulerRequestExecutionService(Protocol):
    def execute(self, context: ContainerExecutionContext) -> ContainerExecutionResult: ...


class WorkerSchedulerRequestResult(ContractModel):
    worker_id: str
    status: WorkerSchedulerRequestStatus
    action: WorkerSchedulerRequestAction
    container_id: str = ""
    request: SchedulerWorkerRequest | None = None
    execution: ContainerExecutionResult | None = None
    image_build: WorkerImageBuildExecutionResult | None = None
    cancelled: WorkerCancelledRequestPlan | None = None
    capacity_released: bool = False
    capacity_release_error: str = ""
    state_deleted: bool = False
    background: bool = False
    error_message: str = ""

    @property
    def processed(self) -> bool:
        return self.status is not WorkerSchedulerRequestStatus.Idle


@dataclass(slots=True)
class _BackgroundExecution:
    request: SchedulerWorkerRequest
    thread: threading.Thread | None = None
    result: WorkerSchedulerRequestResult | None = None


@dataclass(slots=True)
class WorkerSchedulerRequestProcessor:
    worker_id: str
    workers: WorkerSchedulerRequestWorkerRepository
    containers: WorkerSchedulerRequestContainerRepository
    execution: WorkerSchedulerRequestExecutionService
    lifecycle: WorkerSchedulerRequestLifecycle | None = None
    image_builds: WorkerSchedulerRequestImageBuildExecutor | None = None
    _background: dict[str, _BackgroundExecution] = field(default_factory=dict, init=False)

    def run_once(self) -> WorkerSchedulerRequestResult:
        completed = self._pop_completed_background()
        if completed is not None:
            return completed

        request = self.workers.get_next_container_request(self.worker_id)
        if request is None:
            return WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Idle,
                action=WorkerSchedulerRequestAction.Idle,
            )

        state = self.containers.get_container_state(request.container_id)
        cancelled = plan_worker_cancelled_request(
            state_missing=state is None,
            state_status=state.status if state is not None else None,
        )
        if cancelled.drop:
            return self._drop_request(request, cancelled)

        if is_image_build_scheduler_request(request):
            return self._release_capacity(request, self._execute_image_build_request(request))

        try:
            context = container_execution_context_from_scheduler_request(request)
            if runs_in_background(context.startup_kind):
                return self._start_background(request, context)
            execution = self._execute_container(context)
        except Exception as exc:  # pragma: no cover - defensive owner boundary
            result = WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Error,
                action=WorkerSchedulerRequestAction.Execute,
                container_id=request.container_id,
                request=request,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        else:
            result = WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=(
                    WorkerSchedulerRequestStatus.Executed
                    if execution.ok
                    else WorkerSchedulerRequestStatus.Error
                ),
                action=WorkerSchedulerRequestAction.Execute,
                container_id=request.container_id,
                request=request,
                execution=execution,
                error_message=(
                    ""
                    if execution.ok
                    else f"worker execution failed at {execution.failed_phase.value}"
                    if execution.failed_phase is not None
                    else "worker execution failed"
                ),
            )
        return self._release_capacity(request, result)

    def _execute_container(self, context: ContainerExecutionContext) -> ContainerExecutionResult:
        registered = False
        try:
            if self.lifecycle is not None:
                self.lifecycle.register_container(context.request)
                registered = True
            return self.execution.execute(context)
        finally:
            if registered and self.lifecycle is not None:
                self.lifecycle.unregister_container(context.request.container_id)

    def _start_background(
        self,
        request: SchedulerWorkerRequest,
        context: ContainerExecutionContext,
    ) -> WorkerSchedulerRequestResult:
        active = _BackgroundExecution(request=request)
        thread = threading.Thread(
            target=self._run_background,
            args=(active, context),
            name=f"container-exec-{request.container_id}",
            daemon=True,
        )
        active.thread = thread
        self._background[request.container_id] = active
        thread.start()
        return WorkerSchedulerRequestResult(
            worker_id=self.worker_id,
            status=WorkerSchedulerRequestStatus.Executed,
            action=WorkerSchedulerRequestAction.Execute,
            container_id=request.container_id,
            request=request,
            background=True,
        )

    def _run_background(
        self,
        active: _BackgroundExecution,
        context: ContainerExecutionContext,
    ) -> None:
        try:
            execution = self._execute_container(context)
        except Exception as exc:  # pragma: no cover - defensive owner boundary
            result = WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Error,
                action=WorkerSchedulerRequestAction.Execute,
                container_id=active.request.container_id,
                request=active.request,
                background=True,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        else:
            result = WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=(
                    WorkerSchedulerRequestStatus.Executed
                    if execution.ok
                    else WorkerSchedulerRequestStatus.Error
                ),
                action=WorkerSchedulerRequestAction.Execute,
                container_id=active.request.container_id,
                request=active.request,
                execution=execution,
                background=True,
                error_message=(
                    ""
                    if execution.ok
                    else f"worker execution failed at {execution.failed_phase.value}"
                    if execution.failed_phase is not None
                    else "worker execution failed"
                ),
            )
        active.result = self._release_capacity(active.request, result)

    def _pop_completed_background(self) -> WorkerSchedulerRequestResult | None:
        for container_id, active in tuple(self._background.items()):
            thread = active.thread
            if thread is not None and thread.is_alive():
                continue
            if thread is not None:
                thread.join(timeout=0)
            self._background.pop(container_id, None)
            if active.result is not None:
                return active.result
            return WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Error,
                action=WorkerSchedulerRequestAction.Execute,
                container_id=container_id,
                request=active.request,
                background=True,
                error_message="background container execution ended without a result",
            )
        return None

    def _execute_image_build_request(
        self,
        request: SchedulerWorkerRequest,
    ) -> WorkerSchedulerRequestResult:
        if self.image_builds is None:
            return WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Error,
                action=WorkerSchedulerRequestAction.Execute,
                container_id=request.container_id,
                request=request,
                error_message="image build executor is not configured",
            )
        try:
            self.containers.update_container_status(
                request.container_id,
                SchedulerContainerStatus.Running,
                ttl_seconds=DEFAULT_CONTAINER_STATE_TTL_SECONDS,
            )
            image_build = self.image_builds.execute(request)
            self.containers.set_exit_code(
                request.container_id,
                0 if image_build.ok else 1,
                termination_reason=StopContainerReason.Unknown,
            )
            self.containers.update_container_status(
                request.container_id,
                SchedulerContainerStatus.Complete
                if image_build.ok
                else SchedulerContainerStatus.Failed,
                ttl_seconds=DEFAULT_CONTAINER_STATE_TTL_SECONDS,
            )
        except Exception as exc:  # pragma: no cover - defensive owner boundary
            try:
                self.containers.set_exit_code(
                    request.container_id,
                    1,
                    termination_reason=StopContainerReason.Unknown,
                )
                self.containers.update_container_status(
                    request.container_id,
                    SchedulerContainerStatus.Failed,
                    ttl_seconds=DEFAULT_CONTAINER_STATE_TTL_SECONDS,
                )
            except Exception:
                pass
            return WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Error,
                action=WorkerSchedulerRequestAction.Execute,
                container_id=request.container_id,
                request=request,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        return WorkerSchedulerRequestResult(
            worker_id=self.worker_id,
            status=(
                WorkerSchedulerRequestStatus.Executed
                if image_build.ok
                else WorkerSchedulerRequestStatus.Error
            ),
            action=WorkerSchedulerRequestAction.Execute,
            container_id=request.container_id,
            request=request,
            image_build=image_build,
            error_message=image_build.error_message,
        )

    def _drop_request(
        self,
        request: SchedulerWorkerRequest,
        cancelled: WorkerCancelledRequestPlan,
    ) -> WorkerSchedulerRequestResult:
        state_deleted = False
        if cancelled.delete_state:
            state_deleted = self.containers.delete_container_state(request.container_id)
        action = (
            WorkerSchedulerRequestAction.DropMissingState
            if cancelled.action is WorkerCancelledRequestAction.DropMissingState
            else WorkerSchedulerRequestAction.DropStoppingState
        )
        result = WorkerSchedulerRequestResult(
            worker_id=self.worker_id,
            status=WorkerSchedulerRequestStatus.Dropped,
            action=action,
            container_id=request.container_id,
            request=request,
            cancelled=cancelled,
            state_deleted=state_deleted,
        )
        return self._release_capacity(request, result)

    def _release_capacity(
        self,
        request: SchedulerWorkerRequest,
        result: WorkerSchedulerRequestResult,
    ) -> WorkerSchedulerRequestResult:
        try:
            self.workers.update_worker_capacity(
                self.worker_id,
                request,
                WorkerCapacityChange.Add,
            )
        except Exception as exc:  # pragma: no cover - defensive owner boundary
            return result.model_copy(
                update={
                    "status": WorkerSchedulerRequestStatus.Error,
                    "capacity_release_error": f"{type(exc).__name__}: {exc}",
                }
            )
        return result.model_copy(update={"capacity_released": True})


def container_execution_context_from_scheduler_request(
    request: SchedulerWorkerRequest,
) -> ContainerExecutionContext:
    payload = WorkerContainerRequestPayload.model_validate(request.payload)
    memory_limit_bytes = payload.memory_limit_bytes
    if memory_limit_bytes is None and request.memory_mib > 0:
        memory_limit_bytes = request.memory_mib * MIB
    return ContainerExecutionContext(
        request=ContainerRequestContext(
            container_id=request.container_id,
            image_id=payload.image_id,
            archive_sha256=payload.archive_sha256,
            stub_id=request.stub_id,
            stub_type=payload.stub_type,
            workspace_id=request.workspace_id,
            workspace_name=payload.workspace_name,
            app_id=payload.app_id,
            deployment_id=payload.deployment_id,
            env=list(payload.env),
            secret_names=list(payload.secret_names),
            gateway_token_required=payload.gateway_token_required,
            workspace_storage_required=payload.workspace_storage_required,
            mounts=list(payload.mounts),
            workspace_storage_available=payload.workspace_storage_available,
            workspace_storage_base_mount_path=payload.workspace_storage_base_mount_path,
            cpu_millicores=request.cpu_millicores,
            memory_mib=request.memory_mib,
            disk_limit_bytes=payload.disk_limit_bytes or 0,
            gpu=request.gpu_type,
            gpu_count=gpu_count_for_capacity(
                request.gpu_type,
                request.gpu_request,
                request.gpu_count,
            ),
            cost_per_ms=payload.cost_per_ms,
        ),
        architecture=LinuxArchitecture(request.architecture),
        startup_kind=payload.startup_kind,
        ports=list(payload.ports),
        requested_ports=list(payload.requested_ports),
        checkpoint_exposed_ports=list(payload.checkpoint_exposed_ports),
        checkpoint_id=payload.checkpoint_id,
        checkpoint_enabled=payload.checkpoint_enabled,
        checkpoint_readiness_path=payload.checkpoint_readiness_path,
        checkpoint_readiness_port=payload.checkpoint_readiness_port,
        checkpoint_readiness_timeout_seconds=payload.checkpoint_readiness_timeout_seconds,
        checkpoint_readiness_interval_seconds=payload.checkpoint_readiness_interval_seconds,
        entrypoint=list(payload.entrypoint),
        cwd=payload.cwd,
        runtime=payload.runtime,
        docker_enabled=payload.docker_enabled,
        block_network=payload.block_network,
        allow_list=list(payload.allow_list),
        memory_enforced=payload.memory_enforced,
        memory_limit_bytes=memory_limit_bytes,
        cgroup_path=payload.cgroup_path,
        run_delayed_cleanup=payload.run_delayed_cleanup,
    )


def runs_in_background(kind: WorkerStartupKind) -> bool:
    return kind in {
        WorkerStartupKind.Function,
        WorkerStartupKind.Endpoint,
        WorkerStartupKind.Asgi,
        WorkerStartupKind.TaskQueue,
        WorkerStartupKind.Pod,
        WorkerStartupKind.PodRun,
        WorkerStartupKind.Sandbox,
    }
