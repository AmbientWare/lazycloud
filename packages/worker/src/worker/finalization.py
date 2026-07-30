from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import Field
from shared.contracts import ContractModel
from shared.scheduling import ContainerStatusUpdatePlan, SchedulerContainerStatus

from worker.events import (
    ContainerExecutionPhase,
    ContainerExitCode,
    ContainerRequestContext,
    StopContainerReason,
    normalize_container_exit_code,
)
from worker.status import CONTAINER_STATE_TTL_WHILE_PENDING_SECONDS


class ContainerFinalizationStep(StrEnum):
    SetExitCode = "set-exit-code"
    ReleaseGpu = "release-gpu"
    TeardownNetwork = "teardown-network"
    RemoveUploads = "remove-uploads"
    RemoveSourceWorkspace = "remove-source-workspace"
    MarkStopping = "mark-stopping"
    ForceKillIfRunning = "force-kill-if-running"
    StopOomWatcher = "stop-oom-watcher"
    UnmountRequestMounts = "unmount-request-mounts"
    ReleaseContainerRootfs = "release-container-rootfs"
    DeleteLocalState = "delete-local-state"
    DeleteRemoteState = "delete-remote-state"


class ContainerStatusUpdater(Protocol):
    def update_container_status(
        self,
        container_id: str,
        status: SchedulerContainerStatus,
        *,
        ttl_seconds: int,
    ) -> ContainerStatusUpdatePlan: ...


class ContainerFinalizationRepository(ContainerStatusUpdater, Protocol):
    def set_exit_code(
        self,
        container_id: str,
        exit_code: int,
        *,
        termination_reason: StopContainerReason,
        failed_phase: ContainerExecutionPhase | None = None,
        failure_detail: str = "",
    ) -> None: ...

    def delete_container_state(self, container_id: str) -> bool: ...


class ContainerFinalizationCleanup(Protocol):
    def release_gpu(self, container_id: str) -> None: ...

    def teardown_network(self, container_id: str) -> None: ...

    def remove_uploads(self, container_id: str) -> None: ...

    def remove_source_workspace(self, container_id: str) -> None: ...

    def force_stop_if_running(self, container_id: str) -> None: ...

    def stop_oom_watcher(self, container_id: str) -> None: ...

    def unmount_request_mounts(self, container_id: str) -> None: ...

    def release_container_rootfs(self, container_id: str) -> None: ...

    def delete_local_state(self, container_id: str) -> None: ...


class ContainerFinalizationRequest(ContractModel):
    request: ContainerRequestContext
    exit_code: int
    stop_reason: StopContainerReason = StopContainerReason.Unknown
    oom_killed: bool = False
    failed_phase: ContainerExecutionPhase | None = None
    failure_detail: str = ""
    stopping_ttl_seconds: int = CONTAINER_STATE_TTL_WHILE_PENDING_SECONDS


class ContainerFinalizationPlan(ContractModel):
    container_id: str
    normalized_exit_code: int
    stop_reason: StopContainerReason
    failed_phase: ContainerExecutionPhase | None = None
    failure_detail: str = ""
    release_gpu: bool = False
    mark_stopping: bool = True
    remove_uploads: bool = True
    remove_source_workspace: bool = True
    teardown_network: bool = True
    force_kill_after_grace: bool = True
    stop_oom_watcher: bool = True
    release_container_rootfs: bool = True
    delete_local_state: bool = True
    delete_remote_state: bool = True
    stopping_ttl_seconds: int = CONTAINER_STATE_TTL_WHILE_PENDING_SECONDS


class ContainerFinalizationStepResult(ContractModel):
    step: ContainerFinalizationStep
    ok: bool = True
    skipped: bool = False
    error_message: str = ""


class ContainerFinalizationResult(ContractModel):
    plan: ContainerFinalizationPlan
    steps: list[ContainerFinalizationStepResult] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    @property
    def errors(self) -> dict[str, str]:
        return {step.step.value: step.error_message for step in self.steps if step.error_message}


@dataclass(slots=True)
class WorkerContainerFinalizationService:
    repository: ContainerFinalizationRepository
    cleanup: ContainerFinalizationCleanup

    def finalize(
        self,
        request: ContainerFinalizationRequest,
    ) -> ContainerFinalizationResult:
        plan = plan_container_finalization(request)
        steps = [
            self._run_step(
                ContainerFinalizationStep.SetExitCode,
                lambda: self.repository.set_exit_code(
                    plan.container_id,
                    plan.normalized_exit_code,
                    termination_reason=plan.stop_reason,
                    failed_phase=plan.failed_phase,
                    failure_detail=plan.failure_detail,
                ),
            ),
            self._run_step(
                ContainerFinalizationStep.ReleaseGpu,
                lambda: self.cleanup.release_gpu(plan.container_id),
                skip=not plan.release_gpu,
            ),
            self._run_step(
                ContainerFinalizationStep.TeardownNetwork,
                lambda: self.cleanup.teardown_network(plan.container_id),
                skip=not plan.teardown_network,
            ),
            self._run_step(
                ContainerFinalizationStep.RemoveUploads,
                lambda: self.cleanup.remove_uploads(plan.container_id),
                skip=not plan.remove_uploads,
            ),
            self._run_step(
                ContainerFinalizationStep.RemoveSourceWorkspace,
                lambda: self.cleanup.remove_source_workspace(plan.container_id),
                skip=not plan.remove_source_workspace,
            ),
            self._run_step(
                ContainerFinalizationStep.MarkStopping,
                lambda: self.repository.update_container_status(
                    plan.container_id,
                    SchedulerContainerStatus.Stopping,
                    ttl_seconds=plan.stopping_ttl_seconds,
                ),
                skip=not plan.mark_stopping,
            ),
        ]
        return ContainerFinalizationResult(plan=plan, steps=steps)

    def complete_delayed_cleanup(
        self,
        plan: ContainerFinalizationPlan,
    ) -> ContainerFinalizationResult:
        steps = [
            self._run_step(
                ContainerFinalizationStep.ForceKillIfRunning,
                lambda: self.cleanup.force_stop_if_running(plan.container_id),
                skip=not plan.force_kill_after_grace,
            ),
            self._run_step(
                ContainerFinalizationStep.StopOomWatcher,
                lambda: self.cleanup.stop_oom_watcher(plan.container_id),
                skip=not plan.stop_oom_watcher,
            ),
            self._run_step(
                ContainerFinalizationStep.UnmountRequestMounts,
                lambda: self.cleanup.unmount_request_mounts(plan.container_id),
            ),
            # After the request mounts and before local state: the overlay must be
            # unmounted before anything removes paths beneath it, or the removal
            # would delete through the mount into the shared image directory.
            self._run_step(
                ContainerFinalizationStep.ReleaseContainerRootfs,
                lambda: self.cleanup.release_container_rootfs(plan.container_id),
                skip=not plan.release_container_rootfs,
            ),
            self._run_step(
                ContainerFinalizationStep.DeleteLocalState,
                lambda: self.cleanup.delete_local_state(plan.container_id),
                skip=not plan.delete_local_state,
            ),
            self._run_step(
                ContainerFinalizationStep.DeleteRemoteState,
                lambda: self.repository.delete_container_state(plan.container_id),
                skip=not plan.delete_remote_state,
            ),
        ]
        return ContainerFinalizationResult(plan=plan, steps=steps)

    def _run_step[StepResult](
        self,
        step: ContainerFinalizationStep,
        action: Callable[[], StepResult],
        *,
        skip: bool = False,
    ) -> ContainerFinalizationStepResult:
        if skip:
            return ContainerFinalizationStepResult(step=step, skipped=True)
        try:
            action()
        except Exception as exc:  # pragma: no cover - defensive boundary capture
            return ContainerFinalizationStepResult(
                step=step,
                ok=False,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        return ContainerFinalizationStepResult(step=step)


def plan_container_finalization(
    request: ContainerFinalizationRequest,
) -> ContainerFinalizationPlan:
    normalized = normalize_container_exit_code(
        request.exit_code,
        stop_reason=request.stop_reason,
        oom_killed=request.oom_killed,
    )
    if request.exit_code == int(ContainerExitCode.Sigterm) and (
        request.stop_reason is StopContainerReason.Unknown
    ):
        normalized = int(ContainerExitCode.Success)
    return ContainerFinalizationPlan(
        container_id=request.request.container_id,
        normalized_exit_code=normalized,
        stop_reason=request.stop_reason,
        failed_phase=request.failed_phase,
        failure_detail=request.failure_detail,
        release_gpu=bool(request.request.gpu or request.request.gpu_count),
        stopping_ttl_seconds=request.stopping_ttl_seconds,
    )
