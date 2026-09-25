from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from threading import Event

from shared.placement import Placement
from shared.scheduling import (
    SchedulerWorkerStatus,
    WorkerExecutionRecord,
    WorkerRemovalResult,
    WorkerUnavailableReason,
)
from worker.worker_lifecycle import (
    WorkerCleanupAction,
    WorkerLifecycleAction,
    WorkerLifecycleOrchestrator,
    WorkerLifecycleStatus,
)


class _HeldRepository:
    """Registration held until the readiness preparation has finished, as a reserve's is."""

    def __init__(self, prepared: Event, *, refuse: bool = False) -> None:
        self.prepared = prepared
        self.refuse = refuse
        self.available = False

    def add_worker(
        self, worker: WorkerExecutionRecord, *, ttl_seconds: int = 0, now: datetime | None = None
    ) -> WorkerExecutionRecord:
        del ttl_seconds, now
        if self.refuse:
            raise RuntimeError("the fence refused this worker")
        if not self.prepared.wait(timeout=5):
            raise AssertionError("readiness preparation did not run beside registration")
        return worker

    def toggle_worker_available(
        self, worker_id: str, *, ttl_seconds: int
    ) -> WorkerExecutionRecord | None:
        del worker_id, ttl_seconds
        self.available = True
        return None

    def set_keep_alive(self, worker_id: str, *, ttl_seconds: int) -> WorkerExecutionRecord | None:
        del worker_id, ttl_seconds
        return None

    def prepare_source_cache(self) -> None:
        return None

    def disable_worker(
        self,
        worker_id: str,
        *,
        reason: WorkerUnavailableReason,
        detail: str = "",
        ttl_seconds: int,
    ) -> WorkerExecutionRecord | None:
        del worker_id, reason, detail, ttl_seconds
        return None

    def remove_worker(self, worker_id: str) -> WorkerRemovalResult:
        return WorkerRemovalResult(worker_id=worker_id, removed=True)


def _lifecycle(
    repository: _HeldRepository, preparer: Callable[[], None]
) -> WorkerLifecycleOrchestrator:
    return WorkerLifecycleOrchestrator(
        worker_id="worker-1",
        route_restorer=lambda: None,
        repository=repository,
        registration=WorkerExecutionRecord(
            worker_id="worker-1",
            machine_id="machine-1",
            placement=Placement.platform(),
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            status=SchedulerWorkerStatus.Pending,
        ),
        readiness_preparer=preparer,
    )


def test_readiness_preparation_finishes_while_registration_is_held() -> None:
    prepared = Event()
    repository = _HeldRepository(prepared)
    runs: list[None] = []

    def prepare() -> None:
        runs.append(None)
        prepared.set()

    lifecycle = _lifecycle(repository, prepare)
    assert all(step.ok for step in lifecycle.register_available())
    assert all(step.ok for step in lifecycle.register_available())

    assert repository.available
    assert len(runs) == 1


def test_shutdown_finishes_the_preparation_before_cleanup_closes_its_network() -> None:
    finished = Event()
    closed_after: list[bool] = []

    def prepare() -> None:
        time.sleep(0.2)
        finished.set()

    lifecycle = _lifecycle(_HeldRepository(Event(), refuse=True), prepare)
    lifecycle.cleanup_actions.append(
        WorkerCleanupAction(name="network", action=lambda: closed_after.append(finished.is_set()))
    )

    assert not all(step.ok for step in lifecycle.register_available())
    lifecycle.shutdown(drain_timeout_seconds=0, remove_worker=False)

    assert closed_after == [True]


def test_a_failed_readiness_preparation_keeps_the_worker_unavailable() -> None:
    prepared = Event()
    repository = _HeldRepository(prepared)

    def fail() -> None:
        prepared.set()
        raise RuntimeError("managed runtime catalog is unavailable")

    steps = _lifecycle(repository, fail).register_available()

    failed = steps[-1]
    assert failed.action is WorkerLifecycleAction.ValidateReadiness
    assert failed.status is WorkerLifecycleStatus.Error
    assert "managed runtime catalog" in failed.error_message
    assert not repository.available
