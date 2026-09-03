from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import Field
from shared.container_requests import StopContainerReason
from shared.contracts import ContractModel
from shared.scheduling import (
    SchedulerWorkerRecord,
    SchedulerWorkerStatus,
    WorkerRemovalResult,
    WorkerUnavailableReason,
)
from shared.timestamps import utc_now

from worker.events import ContainerRequestContext
from worker.repository_client import WorkerSourceCacheNotAvailableError
from worker.status import (
    DEFAULT_WORKER_SPINDOWN_SECONDS,
    WorkerSpindownPlan,
    plan_worker_spindown,
)

DEFAULT_WORKER_KEEPALIVE_TTL_SECONDS = 60
DEFAULT_WORKER_SHUTDOWN_DRAIN_SECONDS = 5.0
DEFAULT_WORKER_STOP_GRACE_SECONDS = 5.0
DEFAULT_WORKER_FORCE_STOP_WAIT_SECONDS = 1.0
DEFAULT_WORKER_SHUTDOWN_REPOSITORY_TIMEOUT_SECONDS = 2.0
DEFAULT_WORKER_CLEANUP_RETRIES = 3
DEFAULT_WORKER_USAGE_INTERVAL_SECONDS = 30.0


class WorkerLifecycleAction(StrEnum):
    MarkAvailable = "mark-available"
    ActivateSourceCache = "activate-source-cache"
    ValidateReadiness = "validate-readiness"
    KeepAlive = "keepalive"
    DisableScheduling = "disable-scheduling"
    DrainRequests = "drain-requests"
    StopActiveContainers = "stop-active-containers"
    ForceStopActiveContainers = "force-stop-active-containers"
    Cleanup = "cleanup"
    RemoveWorker = "remove-worker"
    EmitUsage = "emit-usage"


class WorkerLifecycleStatus(StrEnum):
    Ok = "ok"
    Skipped = "skipped"
    Error = "error"


class WorkerLifecycleRepository(Protocol):
    def add_worker(
        self,
        worker: SchedulerWorkerRecord,
        *,
        ttl_seconds: int = 0,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord: ...

    def toggle_worker_available(
        self,
        worker_id: str,
        *,
        ttl_seconds: int,
    ) -> SchedulerWorkerRecord | None: ...

    def set_keep_alive(
        self,
        worker_id: str,
        *,
        ttl_seconds: int,
    ) -> SchedulerWorkerRecord | None: ...

    def prepare_source_cache(self) -> None: ...

    def disable_worker(
        self,
        worker_id: str,
        *,
        reason: WorkerUnavailableReason,
        detail: str = "",
        ttl_seconds: int,
    ) -> SchedulerWorkerRecord | None: ...

    def remove_worker(self, worker_id: str) -> WorkerRemovalResult: ...


@runtime_checkable
class WorkerLifecycleShutdownRepository(Protocol):
    def prepare_shutdown(self, *, timeout_seconds: float) -> None: ...


class WorkerLifecycleContainerStopper(Protocol):
    def stop_container(
        self,
        container_id: str,
        *,
        force: bool,
        reason: StopContainerReason = StopContainerReason.Unknown,
    ) -> None: ...


class WorkerLifecycleStepResult(ContractModel):
    action: WorkerLifecycleAction
    status: WorkerLifecycleStatus = WorkerLifecycleStatus.Ok
    attempts: int = 1
    container_ids: list[str] = Field(default_factory=list)
    error_message: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is not WorkerLifecycleStatus.Error


class WorkerStartupSlotResult(ContractModel):
    acquired: bool
    active_starts: int
    limit: int
    reason: str = ""


class WorkerShutdownResult(ContractModel):
    worker_id: str
    steps: list[WorkerLifecycleStepResult] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    @property
    def errors(self) -> dict[str, str]:
        return {
            step.action.value: step.error_message
            for step in self.steps
            if step.status is WorkerLifecycleStatus.Error
        }


@dataclass(slots=True)
class WorkerCleanupAction:
    name: str
    action: Callable[[], None]


@dataclass(slots=True)
class WorkerActiveContainer:
    request: ContainerRequestContext
    started_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class WorkerLifecycleOrchestrator:
    worker_id: str
    repository: WorkerLifecycleRepository | None = None
    stopper: WorkerLifecycleContainerStopper | None = None
    registration: SchedulerWorkerRecord | None = None
    readiness_validator: Callable[[], None] | None = None
    cleanup_actions: list[WorkerCleanupAction] = field(default_factory=list)
    startup_concurrency_limit: int = 1
    keepalive_ttl_seconds: int = DEFAULT_WORKER_KEEPALIVE_TTL_SECONDS
    cleanup_retries: int = DEFAULT_WORKER_CLEANUP_RETRIES
    usage_interval_seconds: float = DEFAULT_WORKER_USAGE_INTERVAL_SECONDS
    _draining: bool = False
    _active: dict[str, WorkerActiveContainer] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _start_sem: threading.BoundedSemaphore = field(init=False)
    _active_starts: int = 0

    def __post_init__(self) -> None:
        self.startup_concurrency_limit = max(self.startup_concurrency_limit, 1)
        self._start_sem = threading.BoundedSemaphore(self.startup_concurrency_limit)

    @property
    def draining(self) -> bool:
        return self._draining

    def mark_available(self) -> WorkerLifecycleStepResult:
        repository = self.repository
        if repository is None:
            return WorkerLifecycleStepResult(
                action=WorkerLifecycleAction.MarkAvailable,
                status=WorkerLifecycleStatus.Skipped,
                error_message="worker repository is not configured",
            )
        return self._run_repository_step(
            WorkerLifecycleAction.MarkAvailable,
            lambda: repository.toggle_worker_available(
                self.worker_id,
                ttl_seconds=self.keepalive_ttl_seconds,
            ),
        )

    def register_available(self, *, now: datetime | None = None) -> list[WorkerLifecycleStepResult]:
        registration = self.registration
        if registration is None:
            return [self.mark_available()]
        repository = self.repository
        if repository is None:
            return [
                WorkerLifecycleStepResult(
                    action=WorkerLifecycleAction.MarkAvailable,
                    status=WorkerLifecycleStatus.Skipped,
                    error_message="worker repository is not configured",
                )
            ]
        current_time = now or utc_now()
        registration = registration.model_copy(
            update={
                "status": registration.status,
                "updated_at": current_time,
                "created_at": registration.created_at,
            }
        )
        added = self._run_repository_step(
            WorkerLifecycleAction.MarkAvailable,
            lambda: repository.add_worker(
                registration,
                ttl_seconds=self.keepalive_ttl_seconds,
                now=current_time,
            ),
        )
        if not added.ok:
            return [added]
        activation = self._run_repository_step(
            WorkerLifecycleAction.ActivateSourceCache,
            lambda: repository.prepare_source_cache(),
        )
        if not activation.ok:
            return [added, activation]
        readiness_validator = self.readiness_validator
        if readiness_validator is not None:
            readiness = self._run_repository_step(
                WorkerLifecycleAction.ValidateReadiness,
                readiness_validator,
            )
            if not readiness.ok:
                return [added, readiness]
        else:
            readiness = None
        available = self.mark_available()
        return [
            added,
            activation,
            *([readiness] if readiness is not None else []),
            available,
        ]

    def keepalive(self) -> WorkerLifecycleStepResult:
        if self._draining:
            return WorkerLifecycleStepResult(
                action=WorkerLifecycleAction.KeepAlive,
                status=WorkerLifecycleStatus.Skipped,
                error_message="worker is draining",
            )
        repository = self.repository
        if repository is None:
            return WorkerLifecycleStepResult(
                action=WorkerLifecycleAction.KeepAlive,
                status=WorkerLifecycleStatus.Skipped,
                error_message="worker repository is not configured",
            )
        try:
            worker = repository.set_keep_alive(
                self.worker_id,
                ttl_seconds=self.keepalive_ttl_seconds,
            )
        except WorkerSourceCacheNotAvailableError as exc:
            # The record is still there; only the cache is withholding it.
            # Registering again cannot change that and costs a request every
            # interval for as long as the condition lasts.
            return WorkerLifecycleStepResult(
                action=WorkerLifecycleAction.KeepAlive,
                status=WorkerLifecycleStatus.Skipped,
                error_message=f"source cache is {exc.state.value}",
            )
        except Exception as exc:  # pragma: no cover - defensive boundary capture
            worker = None
            result = WorkerLifecycleStepResult(
                action=WorkerLifecycleAction.KeepAlive,
                status=WorkerLifecycleStatus.Error,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        else:
            result = WorkerLifecycleStepResult(action=WorkerLifecycleAction.KeepAlive)
        if self.registration is None or (
            result.ok and (worker is None or worker.status is not SchedulerWorkerStatus.Pending)
        ):
            return result
        registered = self.register_available()
        if all(step.ok for step in registered):
            return WorkerLifecycleStepResult(
                action=WorkerLifecycleAction.KeepAlive,
                attempts=result.attempts + sum(step.attempts for step in registered),
                metadata={"re_registered": "true"},
            )
        if result.ok:
            detail = "; ".join(
                f"{step.action.value}: {step.error_message}" for step in registered if not step.ok
            )
            return WorkerLifecycleStepResult(
                action=WorkerLifecycleAction.KeepAlive,
                status=WorkerLifecycleStatus.Error,
                attempts=result.attempts + sum(step.attempts for step in registered),
                error_message=f"worker re-registration failed: {detail}",
            )
        return result

    def disable_scheduling(
        self,
        *,
        reason: WorkerUnavailableReason = WorkerUnavailableReason.ShuttingDown,
        detail: str = "",
    ) -> WorkerLifecycleStepResult:
        self._draining = True
        repository = self.repository
        if repository is None:
            return WorkerLifecycleStepResult(
                action=WorkerLifecycleAction.DisableScheduling,
                status=WorkerLifecycleStatus.Skipped,
                error_message="worker repository is not configured",
            )
        return self._run_repository_step(
            WorkerLifecycleAction.DisableScheduling,
            lambda: repository.disable_worker(
                self.worker_id,
                reason=reason,
                detail=detail,
                ttl_seconds=self.keepalive_ttl_seconds,
            ),
        )

    def acquire_start_slot(self, *, timeout_seconds: float = 0.0) -> WorkerStartupSlotResult:
        acquired = self._start_sem.acquire(timeout=max(timeout_seconds, 0.0))
        with self._lock:
            if acquired:
                self._active_starts += 1
            return WorkerStartupSlotResult(
                acquired=acquired,
                active_starts=self._active_starts,
                limit=self.startup_concurrency_limit,
                reason="startup slot acquired" if acquired else "startup concurrency limit reached",
            )

    def release_start_slot(self) -> WorkerStartupSlotResult:
        with self._lock:
            if self._active_starts <= 0:
                return WorkerStartupSlotResult(
                    acquired=False,
                    active_starts=0,
                    limit=self.startup_concurrency_limit,
                    reason="no startup slot is held",
                )
            self._active_starts -= 1
            self._start_sem.release()
            return WorkerStartupSlotResult(
                acquired=False,
                active_starts=self._active_starts,
                limit=self.startup_concurrency_limit,
                reason="startup slot released",
            )

    def register_container(
        self,
        request: ContainerRequestContext,
        *,
        started_at: datetime | None = None,
    ) -> None:
        with self._lock:
            current_time = started_at or utc_now()
            self._active[request.container_id] = WorkerActiveContainer(
                request=request,
                started_at=current_time,
            )

    def unregister_container(self, container_id: str) -> None:
        with self._lock:
            self._active.pop(container_id, None)

    def active_container_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._active)

    def spindown_plan(
        self,
        *,
        persistent: bool = False,
        seconds_since_last_request: float = 0.0,
        spindown_seconds: float = DEFAULT_WORKER_SPINDOWN_SECONDS,
    ) -> WorkerSpindownPlan:
        return plan_worker_spindown(
            persistent=persistent,
            seconds_since_last_request=seconds_since_last_request,
            active_container_count=len(self.active_container_ids()),
            spindown_seconds=spindown_seconds,
        )

    def shutdown(
        self,
        *,
        drain_timeout_seconds: float = DEFAULT_WORKER_SHUTDOWN_DRAIN_SECONDS,
        stop_grace_seconds: float = DEFAULT_WORKER_STOP_GRACE_SECONDS,
        force_stop_wait_seconds: float = DEFAULT_WORKER_FORCE_STOP_WAIT_SECONDS,
        repository_timeout_seconds: float = DEFAULT_WORKER_SHUTDOWN_REPOSITORY_TIMEOUT_SECONDS,
        remove_worker: bool = True,
        stop_reason: StopContainerReason = StopContainerReason.Unknown,
        unavailable_reason: WorkerUnavailableReason = WorkerUnavailableReason.ShuttingDown,
        unavailable_detail: str = "",
    ) -> WorkerShutdownResult:
        if isinstance(self.repository, WorkerLifecycleShutdownRepository):
            self.repository.prepare_shutdown(
                timeout_seconds=max(repository_timeout_seconds, 0.1),
            )
        steps = [
            self.disable_scheduling(
                reason=unavailable_reason,
                detail=unavailable_detail,
            )
        ]
        steps.append(
            self._wait_for_active_containers(
                timeout_seconds=max(drain_timeout_seconds, 0.0),
            )
        )
        active_after_drain = self.active_container_ids()
        if active_after_drain:
            steps.append(
                self._stop_active_containers(
                    active_after_drain,
                    force=False,
                    reason=stop_reason,
                )
            )
            steps.append(
                self._wait_for_active_containers(
                    timeout_seconds=max(stop_grace_seconds, 0.0),
                )
            )
        remaining = self.active_container_ids()
        if remaining:
            steps.append(
                self._stop_active_containers(
                    remaining,
                    force=True,
                    reason=stop_reason,
                )
            )
            steps.append(
                self._wait_for_active_containers(
                    timeout_seconds=max(force_stop_wait_seconds, 0.0),
                )
            )
        steps.extend(self._run_cleanup_actions())
        if remove_worker:
            steps.append(self._remove_worker())
        return WorkerShutdownResult(worker_id=self.worker_id, steps=steps)

    def _wait_for_active_containers(self, *, timeout_seconds: float) -> WorkerLifecycleStepResult:
        deadline = time.monotonic() + timeout_seconds
        while self.active_container_ids() and time.monotonic() < deadline:
            time.sleep(0.05)
        remaining = self.active_container_ids()
        return WorkerLifecycleStepResult(
            action=WorkerLifecycleAction.DrainRequests,
            status=(WorkerLifecycleStatus.Ok if not remaining else WorkerLifecycleStatus.Skipped),
            container_ids=remaining,
            error_message="" if not remaining else "active containers remain after drain timeout",
        )

    def _stop_active_containers(
        self,
        container_ids: Iterable[str],
        *,
        force: bool,
        reason: StopContainerReason,
    ) -> WorkerLifecycleStepResult:
        ids = list(dict.fromkeys(container_ids))
        if self.stopper is None:
            return WorkerLifecycleStepResult(
                action=(
                    WorkerLifecycleAction.ForceStopActiveContainers
                    if force
                    else WorkerLifecycleAction.StopActiveContainers
                ),
                status=WorkerLifecycleStatus.Skipped,
                container_ids=ids,
                error_message="container stopper is not configured",
            )
        errors: list[str] = []
        for container_id in ids:
            try:
                self.stopper.stop_container(container_id, force=force, reason=reason)
            except Exception as exc:  # pragma: no cover - defensive boundary capture
                errors.append(f"{container_id}: {type(exc).__name__}: {exc}")
        return WorkerLifecycleStepResult(
            action=(
                WorkerLifecycleAction.ForceStopActiveContainers
                if force
                else WorkerLifecycleAction.StopActiveContainers
            ),
            status=WorkerLifecycleStatus.Error if errors else WorkerLifecycleStatus.Ok,
            container_ids=ids,
            error_message="; ".join(errors),
        )

    def _run_cleanup_actions(self) -> list[WorkerLifecycleStepResult]:
        return [
            self._run_cleanup_action(action, retries=max(self.cleanup_retries, 1))
            for action in self.cleanup_actions
        ]

    def _run_cleanup_action(
        self,
        cleanup: WorkerCleanupAction,
        *,
        retries: int,
    ) -> WorkerLifecycleStepResult:
        error = ""
        for attempt in range(1, retries + 1):
            try:
                cleanup.action()
                return WorkerLifecycleStepResult(
                    action=WorkerLifecycleAction.Cleanup,
                    attempts=attempt,
                    metadata={"name": cleanup.name},
                )
            except Exception as exc:  # pragma: no cover - defensive boundary capture
                error = f"{type(exc).__name__}: {exc}"
        return WorkerLifecycleStepResult(
            action=WorkerLifecycleAction.Cleanup,
            status=WorkerLifecycleStatus.Error,
            attempts=retries,
            error_message=error,
            metadata={"name": cleanup.name},
        )

    def _remove_worker(self) -> WorkerLifecycleStepResult:
        repository = self.repository
        if repository is None:
            return WorkerLifecycleStepResult(
                action=WorkerLifecycleAction.RemoveWorker,
                status=WorkerLifecycleStatus.Skipped,
                error_message="worker repository is not configured",
            )
        return self._run_repository_step(
            WorkerLifecycleAction.RemoveWorker,
            lambda: repository.remove_worker(self.worker_id),
        )

    def _run_repository_step(
        self,
        action: WorkerLifecycleAction,
        callback: Callable[[], SchedulerWorkerRecord | WorkerRemovalResult | None],
    ) -> WorkerLifecycleStepResult:
        try:
            result = callback()
        except Exception as exc:  # pragma: no cover - defensive boundary capture
            return WorkerLifecycleStepResult(
                action=action,
                status=WorkerLifecycleStatus.Error,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        metadata: dict[str, str] = {}
        if isinstance(result, WorkerRemovalResult):
            metadata["requeued_count"] = str(result.requeued_count)
        return WorkerLifecycleStepResult(action=action, metadata=metadata)
