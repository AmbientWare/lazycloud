from __future__ import annotations

from dataclasses import dataclass, field

from scheduler.state import ContainerStatusUpdatePlan, SchedulerContainerStatus
from worker.events import (
    ContainerExecutionPhase,
    ContainerExitCode,
    ContainerRequestContext,
    StopContainerReason,
)
from worker.finalization import (
    ContainerFinalizationRequest,
    ContainerFinalizationStep,
    WorkerContainerFinalizationService,
    plan_container_finalization,
)
from worker.status import CONTAINER_STATE_TTL_WHILE_PENDING_SECONDS


@dataclass(slots=True)
class FinalizationRepository:
    exit_codes: list[tuple[str, int, StopContainerReason]] = field(default_factory=list)
    failure_details: list[tuple[ContainerExecutionPhase | None, str]] = field(default_factory=list)
    status_updates: list[tuple[str, SchedulerContainerStatus, int]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    storage_released: bool = False

    def set_exit_code(
        self,
        container_id: str,
        exit_code: int,
        *,
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
        self.status_updates.append((container_id, status, ttl_seconds))
        return ContainerStatusUpdatePlan(
            container_id=container_id,
            previous_status=SchedulerContainerStatus.Running,
            next_status=status,
            changed=True,
            ttl_seconds=ttl_seconds,
        )

    def delete_container_state(self, container_id: str, *, storage_released: bool = False) -> bool:
        self.storage_released = storage_released
        self.deleted.append(container_id)
        return True


@dataclass(slots=True)
class FinalizationCleanup:
    calls: list[tuple[ContainerFinalizationStep, str]] = field(default_factory=list)
    fail_step: ContainerFinalizationStep | None = None

    def release_gpu(self, container_id: str) -> None:
        self._record(ContainerFinalizationStep.ReleaseGpu, container_id)

    def teardown_network(self, container_id: str) -> None:
        self._record(ContainerFinalizationStep.TeardownNetwork, container_id)

    def remove_uploads(self, container_id: str) -> None:
        self._record(ContainerFinalizationStep.RemoveUploads, container_id)

    def remove_source_workspace(self, container_id: str) -> None:
        self._record(ContainerFinalizationStep.RemoveSourceWorkspace, container_id)

    def force_stop_if_running(self, container_id: str) -> None:
        self._record(ContainerFinalizationStep.ForceKillIfRunning, container_id)

    def stop_oom_watcher(self, container_id: str) -> None:
        self._record(ContainerFinalizationStep.StopOomWatcher, container_id)

    def unmount_request_mounts(self, container_id: str) -> None:
        self._record(ContainerFinalizationStep.UnmountRequestMounts, container_id)

    def release_container_rootfs(self, container_id: str) -> None:
        self._record(ContainerFinalizationStep.ReleaseContainerRootfs, container_id)

    def delete_local_state(self, container_id: str) -> None:
        self._record(ContainerFinalizationStep.DeleteLocalState, container_id)

    def _record(self, step: ContainerFinalizationStep, container_id: str) -> None:
        if self.fail_step is step:
            msg = f"{step.value} failed"
            raise RuntimeError(msg)
        self.calls.append((step, container_id))


def test_worker_container_finalizer_records_exit_and_immediate_cleanup() -> None:
    repo = FinalizationRepository()
    cleanup = FinalizationCleanup()
    service = WorkerContainerFinalizationService(repository=repo, cleanup=cleanup)
    request = ContainerFinalizationRequest(
        request=ContainerRequestContext(container_id="ctr-1", gpu="L4", gpu_count=1),
        exit_code=-1,
    )

    result = service.finalize(request)

    assert result.ok
    assert result.plan.normalized_exit_code == int(ContainerExitCode.UnknownError)
    assert repo.exit_codes == [
        ("ctr-1", int(ContainerExitCode.UnknownError), StopContainerReason.Unknown)
    ]
    assert repo.status_updates == [
        ("ctr-1", SchedulerContainerStatus.Stopping, CONTAINER_STATE_TTL_WHILE_PENDING_SECONDS)
    ]
    assert cleanup.calls == [
        (ContainerFinalizationStep.ReleaseGpu, "ctr-1"),
        (ContainerFinalizationStep.TeardownNetwork, "ctr-1"),
        (ContainerFinalizationStep.RemoveUploads, "ctr-1"),
        (ContainerFinalizationStep.RemoveSourceWorkspace, "ctr-1"),
    ]


def test_worker_container_finalizer_delayed_cleanup_forces_and_deletes_state() -> None:
    repo = FinalizationRepository()
    cleanup = FinalizationCleanup()
    service = WorkerContainerFinalizationService(repository=repo, cleanup=cleanup)
    plan = plan_container_finalization(
        ContainerFinalizationRequest(
            request=ContainerRequestContext(container_id="ctr-1"),
            exit_code=int(ContainerExitCode.Sigterm),
        )
    )

    result = service.complete_delayed_cleanup(plan)

    assert result.ok
    assert plan.normalized_exit_code == int(ContainerExitCode.Success)
    assert cleanup.calls == [
        (ContainerFinalizationStep.ForceKillIfRunning, "ctr-1"),
        (ContainerFinalizationStep.StopOomWatcher, "ctr-1"),
        (ContainerFinalizationStep.UnmountRequestMounts, "ctr-1"),
        (ContainerFinalizationStep.ReleaseContainerRootfs, "ctr-1"),
        (ContainerFinalizationStep.DeleteLocalState, "ctr-1"),
    ]
    assert repo.deleted == ["ctr-1"]
    assert repo.storage_released


def test_failed_unmount_keeps_storage_owned_until_cleanup_succeeds() -> None:
    repo = FinalizationRepository()
    cleanup = FinalizationCleanup(fail_step=ContainerFinalizationStep.UnmountRequestMounts)
    service = WorkerContainerFinalizationService(repository=repo, cleanup=cleanup)
    plan = plan_container_finalization(
        ContainerFinalizationRequest(
            request=ContainerRequestContext(container_id="ctr-1"), exit_code=0
        )
    )

    assert not service.complete_delayed_cleanup(plan).ok
    assert not repo.storage_released
    assert not repo.deleted

    cleanup.fail_step = None
    assert service.complete_delayed_cleanup(plan).ok
    assert repo.storage_released


def test_worker_container_finalizer_uses_stop_reason_exit_code_and_skips_gpu_release() -> None:
    repo = FinalizationRepository()
    cleanup = FinalizationCleanup()
    service = WorkerContainerFinalizationService(repository=repo, cleanup=cleanup)
    request = ContainerFinalizationRequest(
        request=ContainerRequestContext(container_id="ctr-1"),
        exit_code=0,
        stop_reason=StopContainerReason.Admin,
    )

    result = service.finalize(request)

    assert result.plan.normalized_exit_code == int(ContainerExitCode.Admin)
    release_gpu = next(
        step for step in result.steps if step.step is ContainerFinalizationStep.ReleaseGpu
    )
    assert release_gpu.skipped
    assert (ContainerFinalizationStep.ReleaseGpu, "ctr-1") not in cleanup.calls


def test_worker_container_finalizer_records_preemption_as_distinct_exit() -> None:
    repo = FinalizationRepository()
    service = WorkerContainerFinalizationService(
        repository=repo,
        cleanup=FinalizationCleanup(),
    )

    result = service.finalize(
        ContainerFinalizationRequest(
            request=ContainerRequestContext(container_id="ctr-preempted"),
            exit_code=int(ContainerExitCode.Sigterm),
            stop_reason=StopContainerReason.Preempted,
        )
    )

    assert result.plan.normalized_exit_code == int(ContainerExitCode.Preempted)
    assert repo.exit_codes == [
        ("ctr-preempted", int(ContainerExitCode.Preempted), StopContainerReason.Preempted)
    ]


def test_worker_container_finalizer_captures_cleanup_errors_and_continues() -> None:
    repo = FinalizationRepository()
    cleanup = FinalizationCleanup(fail_step=ContainerFinalizationStep.TeardownNetwork)
    service = WorkerContainerFinalizationService(repository=repo, cleanup=cleanup)
    request = ContainerFinalizationRequest(
        request=ContainerRequestContext(container_id="ctr-1", gpu_count=1),
        exit_code=0,
    )

    result = service.finalize(request)

    assert not result.ok
    assert "teardown-network" in result.errors
    assert cleanup.calls == [
        (ContainerFinalizationStep.ReleaseGpu, "ctr-1"),
        (ContainerFinalizationStep.RemoveUploads, "ctr-1"),
        (ContainerFinalizationStep.RemoveSourceWorkspace, "ctr-1"),
    ]
    assert repo.status_updates
