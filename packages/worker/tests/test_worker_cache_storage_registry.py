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


def test_worker_lifecycle_orchestrates_keepalive_shutdown_usage_and_cleanup() -> None:
    repo = _FakeLifecycleRepo()
    stopper = _FakeStopper()
    usage = _FakeUsageEmitter()
    cleanup_attempts = {"count": 0}

    def flaky_cleanup() -> None:
        cleanup_attempts["count"] += 1
        if cleanup_attempts["count"] == 1:
            raise RuntimeError("busy")

    lifecycle = WorkerLifecycleOrchestrator(
        worker_id="worker-1",
        repository=repo,
        stopper=stopper,
        usage_emitter=usage,
        startup_concurrency_limit=1,
        cleanup_retries=2,
        cleanup_actions=[WorkerCleanupAction(name="container", action=flaky_cleanup)],
        usage_interval_seconds=1,
    )
    first_slot = lifecycle.acquire_start_slot()
    second_slot = lifecycle.acquire_start_slot()
    released = lifecycle.release_start_slot()
    request = ContainerRequestContext(container_id="ctr-1", workspace_id="workspace-1")
    lifecycle.register_container(request, started_at=utc_now() - timedelta(seconds=5))

    available = lifecycle.mark_available()
    keepalive = lifecycle.keepalive()
    usage_step = lifecycle.emit_periodic_usage(now=utc_now(), force=True)
    shutdown = lifecycle.shutdown(drain_timeout_seconds=0, stop_grace_seconds=0)

    assert first_slot.acquired
    assert not second_slot.acquired
    assert released.active_starts == 0
    assert available.status is WorkerLifecycleStatus.Ok
    assert keepalive.status is WorkerLifecycleStatus.Ok
    assert usage_step.container_ids == ["ctr-1"]
    assert len(usage.calls) == 2
    (
        container_id,
        duration_ms,
        window_start_ms,
        window_end_ms,
        metering_started_at,
        metering_ended_at,
    ) = usage.calls[0]
    assert container_id == "ctr-1"
    assert duration_ms == window_end_ms - window_start_ms
    assert window_start_ms == 0
    assert window_end_ms >= 5000
    assert metering_started_at < metering_ended_at
    (
        next_container_id,
        next_duration_ms,
        next_start_ms,
        next_end_ms,
        next_metering_started_at,
        next_metering_ended_at,
    ) = usage.calls[1]
    assert next_container_id == "ctr-1"
    assert next_duration_ms == next_end_ms - next_start_ms
    assert next_start_ms == window_end_ms
    assert next_end_ms > next_start_ms
    assert next_metering_started_at == metering_ended_at
    assert next_metering_ended_at > next_metering_started_at
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
class _ShutdownAwareLifecycleRepo:
    actions: list[str] = field(default_factory=list)

    def prepare_shutdown(self, *, timeout_seconds: float) -> None:
        self.actions.append(f"prepare:{timeout_seconds}")

    def add_worker(
        self,
        worker: SchedulerWorkerRecord,
        *,
        ttl_seconds: int = 0,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord:
        _ = ttl_seconds, now
        return worker

    def toggle_worker_available(self, worker_id: str, *, ttl_seconds: int) -> None:
        _ = worker_id, ttl_seconds

    def set_keep_alive(self, worker_id: str, *, ttl_seconds: int) -> None:
        _ = worker_id, ttl_seconds

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


@dataclass(slots=True)
class _FakeUsageEmitter:
    calls: list[tuple[str, int, int, int, datetime, datetime]] = field(default_factory=list)

    def emit_usage(
        self,
        request: ContainerRequestContext,
        *,
        duration_ms: int,
        window_start_ms: int = 0,
        window_end_ms: int | None = None,
        metering_window_started_at: datetime,
        metering_window_ended_at: datetime,
    ) -> None:
        end_ms = window_start_ms + duration_ms if window_end_ms is None else window_end_ms
        self.calls.append(
            (
                request.container_id,
                duration_ms,
                window_start_ms,
                end_ms,
                metering_window_started_at,
                metering_window_ended_at,
            )
        )
