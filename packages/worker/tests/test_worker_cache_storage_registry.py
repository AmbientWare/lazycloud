from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from shared.compute_policy import MachinePool
from shared.scheduling import (
    SchedulerWorkerStatus,
    WorkerExecutionRecord,
    WorkerRemovalResult,
    WorkerUnavailableReason,
)
from worker.worker_lifecycle import (
    WorkerLifecycleOrchestrator,
)

_CAPACITY_OWNER_ID = "11111111-1111-4111-8111-111111111111"


def test_pending_worker_registers_again_after_keepalive_recovers() -> None:
    worker = WorkerExecutionRecord(
        worker_id="worker-1",
        pool=MachinePool("default"),
        capacity_owner_id=_CAPACITY_OWNER_ID,
        status=SchedulerWorkerStatus.Pending,
    )
    repo = _FakeLifecycleRepo(worker=worker)
    lifecycle = WorkerLifecycleOrchestrator(
        worker_id=worker.worker_id,
        repository=repo,
        registration=worker,
    )

    result = lifecycle.keepalive()

    assert result.ok
    assert result.metadata == {"re_registered": "true"}
    assert repo.worker is not None
    assert repo.worker.status is SchedulerWorkerStatus.Available


@dataclass(slots=True)
class _FakeLifecycleRepo:
    actions: list[str] = field(default_factory=list)
    registered: list[WorkerExecutionRecord] = field(default_factory=list)
    fail_next_keepalive: bool = False
    worker: WorkerExecutionRecord | None = None

    def add_worker(
        self,
        worker: WorkerExecutionRecord,
        *,
        ttl_seconds: int = 0,
        now: datetime | None = None,
    ) -> WorkerExecutionRecord:
        _ = ttl_seconds, now
        self.actions.append("registered")
        self.registered.append(worker)
        self.worker = worker
        return worker

    def toggle_worker_available(
        self,
        worker_id: str,
        *,
        ttl_seconds: int,
    ) -> WorkerExecutionRecord | None:
        _ = worker_id, ttl_seconds
        self.actions.append("available")
        if self.worker is not None:
            self.worker = self.worker.model_copy(update={"status": SchedulerWorkerStatus.Available})
        return self.worker

    def set_keep_alive(
        self,
        worker_id: str,
        *,
        ttl_seconds: int,
    ) -> WorkerExecutionRecord | None:
        _ = ttl_seconds
        self.actions.append("keepalive")
        if self.fail_next_keepalive:
            self.fail_next_keepalive = False
            msg = f"worker {worker_id!r} state is missing"
            raise RuntimeError(msg)
        return self.worker

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
