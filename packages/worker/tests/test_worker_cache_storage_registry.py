from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from scheduler.state import SchedulerWorkerRecord, WorkerRemovalResult
from shared.container_requests import StopContainerReason
from shared.scheduling import WorkerUnavailableReason
from shared.timestamps import utc_now
from worker.events import ContainerRequestContext
from worker.worker_lifecycle import (
    WorkerCleanupAction,
    WorkerLifecycleAction,
    WorkerLifecycleOrchestrator,
    WorkerLifecycleStatus,
)

_CAPACITY_OWNER_ID = "11111111-1111-4111-8111-111111111111"


def test_worker_lifecycle_orchestrates_keepalive_shutdown_and_cleanup() -> None:
    repo = _FakeLifecycleRepo()
    stopper = _FakeStopper()
    cleanup_attempts = {"count": 0}

    def flaky_cleanup() -> None:
        cleanup_attempts["count"] += 1
        if cleanup_attempts["count"] == 1:
            raise RuntimeError("busy")

    lifecycle = WorkerLifecycleOrchestrator(
        worker_id="worker-1",
        repository=repo,
        stopper=stopper,
        startup_concurrency_limit=1,
        cleanup_retries=2,
        cleanup_actions=[WorkerCleanupAction(name="container", action=flaky_cleanup)],
    )
    first_slot = lifecycle.acquire_start_slot()
    second_slot = lifecycle.acquire_start_slot()
    released = lifecycle.release_start_slot()
    request = ContainerRequestContext(container_id="ctr-1", workspace_id="workspace-1")
    lifecycle.register_container(request, started_at=utc_now() - timedelta(seconds=5))

    available = lifecycle.mark_available()
    keepalive = lifecycle.keepalive()
    shutdown = lifecycle.shutdown(drain_timeout_seconds=0, stop_grace_seconds=0)

    assert first_slot.acquired
    assert not second_slot.acquired
    assert released.active_starts == 0
    assert available.status is WorkerLifecycleStatus.Ok
    assert keepalive.status is WorkerLifecycleStatus.Ok
    assert repo.actions[:3] == ["available", "keepalive", "disabled"]
    assert ("ctr-1", False) in stopper.calls
    assert ("ctr-1", True) in stopper.calls
    assert cleanup_attempts["count"] == 2
    assert shutdown.ok
    assert shutdown.steps[-1].action is WorkerLifecycleAction.RemoveWorker


@dataclass(slots=True)
class _FakeLifecycleRepo:
    actions: list[str] = field(default_factory=list)
    registered: list[SchedulerWorkerRecord] = field(default_factory=list)
    fail_next_keepalive: bool = False

    def add_worker(
        self,
        worker: SchedulerWorkerRecord,
        *,
        ttl_seconds: int = 0,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord:
        _ = ttl_seconds, now
        self.actions.append("registered")
        self.registered.append(worker)
        return worker

    def toggle_worker_available(self, worker_id: str, *, ttl_seconds: int) -> None:
        _ = worker_id, ttl_seconds
        self.actions.append("available")

    def set_keep_alive(self, worker_id: str, *, ttl_seconds: int) -> None:
        _ = ttl_seconds
        self.actions.append("keepalive")
        if self.fail_next_keepalive:
            self.fail_next_keepalive = False
            msg = f"worker {worker_id!r} state is missing"
            raise RuntimeError(msg)

    def prepare_source_cache(self) -> None:
        self.actions.append("activated")

    def disable_worker(
        self,
        worker_id: str,
        *,
        reason: WorkerUnavailableReason,
        detail: str = "",
        ttl_seconds: int,
    ) -> None:
        _ = worker_id, reason, detail, ttl_seconds
        self.actions.append("disabled")

    def remove_worker(self, worker_id: str) -> WorkerRemovalResult:
        self.actions.append("removed")
        return WorkerRemovalResult(worker_id=worker_id, removed=True)


@dataclass(slots=True)
class _FakeStopper:
    calls: list[tuple[str, bool]] = field(default_factory=list)

    def stop_container(
        self,
        container_id: str,
        *,
        force: bool,
        reason: StopContainerReason = StopContainerReason.Unknown,
    ) -> None:
        del reason
        self.calls.append((container_id, force))
