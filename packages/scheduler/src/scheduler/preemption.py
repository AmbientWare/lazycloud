from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import Field
from shared.compute_enrollment import AgentCapacityState
from shared.compute_policy import MachinePool
from shared.container_requests import StopContainerReason
from shared.contracts import ContractModel
from shared.errors import ConflictError
from shared.scheduling import (
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
)
from shared.timestamps import utc_now


class WorkerPreemptionOperation(ContractModel):
    operation_id: str = Field(min_length=1, max_length=160)
    worker_id: str = Field(min_length=1, max_length=160)
    capacity_owner_id: str = Field(min_length=1, max_length=160)
    machine_id: str = Field(default="", max_length=160)
    expected_resource_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)
    observed_at: datetime


class WorkerPreemptionQueueResult(ContractModel):
    worker: SchedulerWorkerRecord
    changed: bool = False
    requeued_request_ids: list[str] = Field(default_factory=list)


class WorkerPreemptionResult(ContractModel):
    worker: SchedulerWorkerRecord
    changed: bool = False
    requeued_request_ids: list[str] = Field(default_factory=list)
    stopped_container_ids: list[str] = Field(default_factory=list)


class ContainerEvictionResult(ContractModel):
    container_id: str
    stopped: bool = False
    requeued: bool = False
    reason: str = ""


class CapacityInterruption(ContractModel):
    enrollment_id: str
    credential_generation: int = Field(ge=1)
    workspace_id: str
    pool: MachinePool
    machine_id: str
    state: AgentCapacityState
    reason: str
    observed_at: datetime


class CapacityInterruptionSource(Protocol):
    def list_active_interruptions(self) -> list[CapacityInterruption]: ...


class WorkerPreemptionRepository(Protocol):
    def list_workers_on_machine(self, machine_id: str) -> list[SchedulerWorkerRecord]: ...

    def preempt_worker_requests(
        self,
        operation: WorkerPreemptionOperation,
        *,
        now: datetime,
    ) -> WorkerPreemptionQueueResult: ...

    def has_recoverable_container_request(
        self,
        container_id: str,
        *,
        worker_id: str = "",
    ) -> bool: ...


class WorkerPreemptionContainerRepository(Protocol):
    def list_by_worker(self, worker_id: str) -> list[SchedulerContainerState]: ...

    def is_container_cancelled(self, container_id: str) -> bool: ...


class WorkerPreemptionContainerStopper(Protocol):
    def stop(
        self,
        container_id: str,
        *,
        reason: StopContainerReason,
    ) -> object: ...


class SchedulerWorkerPreemption(Protocol):
    def preempt_worker(
        self,
        operation: WorkerPreemptionOperation,
        *,
        now: datetime | None = None,
    ) -> WorkerPreemptionResult: ...


class SchedulerCapacityInterruption(Protocol):
    def preempt_interruption(
        self,
        interruption: CapacityInterruption,
        *,
        now: datetime | None = None,
    ) -> list[WorkerPreemptionResult]: ...


@dataclass(slots=True)
class SchedulerWorkerPreemptionService:
    workers: WorkerPreemptionRepository
    containers: WorkerPreemptionContainerRepository
    stopper: WorkerPreemptionContainerStopper

    def preempt_worker(
        self,
        operation: WorkerPreemptionOperation,
        *,
        now: datetime | None = None,
    ) -> WorkerPreemptionResult:
        current_time = now or utc_now()
        queued = self.workers.preempt_worker_requests(operation, now=current_time)
        requeued = set(queued.requeued_request_ids)
        active = [
            container
            for container in self.containers.list_by_worker(operation.worker_id)
            if container.container_id not in requeued
            and not self.workers.has_recoverable_container_request(
                container.container_id,
                worker_id=operation.worker_id,
            )
            and container.status
            in {SchedulerContainerStatus.Pending, SchedulerContainerStatus.Running}
            and not self.containers.is_container_cancelled(container.container_id)
        ]
        stopped: list[str] = []
        failures: list[str] = []
        for container in active:
            try:
                self.stopper.stop(
                    container.container_id,
                    reason=StopContainerReason.Preempted,
                )
            except Exception as exc:  # pragma: no cover - defensive process boundary
                failures.append(f"{container.container_id}: {type(exc).__name__}: {exc}")
            else:
                stopped.append(container.container_id)
        if failures:
            raise ConflictError("worker preemption container stop failed: " + "; ".join(failures))
        return WorkerPreemptionResult(
            worker=queued.worker,
            changed=queued.changed,
            requeued_request_ids=queued.requeued_request_ids,
            stopped_container_ids=stopped,
        )

    def evict_container(
        self,
        container_id: str,
        *,
        worker_id: str,
        reason: StopContainerReason = StopContainerReason.MemoryEvicted,
    ) -> ContainerEvictionResult:
        """Stop one container to relieve the machine it is running on.

        One container rather than a whole worker, which is what separates this
        from a machine being reclaimed: the machine is fine and stays serving,
        and only the container that grew furthest past its reservation goes.

        Recoverable work is requeued and lands elsewhere. Everything else stops
        carrying a reason that says the machine ran out rather than that anybody
        intervened, because the container did nothing wrong except grow into
        headroom that stopped being spare.
        """
        if self.containers.is_container_cancelled(container_id):
            return ContainerEvictionResult(
                container_id=container_id,
                reason="container was already cancelled",
            )
        # Asked before the stop. Afterwards the request is gone and every
        # container looks unrecoverable, which would silently discard work that
        # had somewhere else to run.
        requeued = self.workers.has_recoverable_container_request(
            container_id,
            worker_id=worker_id,
        )
        self.stopper.stop(container_id, reason=reason)
        return ContainerEvictionResult(
            container_id=container_id,
            stopped=True,
            requeued=requeued,
            reason=reason.describe(),
        )


@dataclass(slots=True)
class SchedulerCapacityInterruptionService:
    preemption: SchedulerWorkerPreemption
    workers: WorkerPreemptionRepository
    source: CapacityInterruptionSource | None = None

    def reconcile(self, *, now: datetime | None = None) -> list[WorkerPreemptionResult]:
        if self.source is None:
            return []
        current_time = now or utc_now()
        results: list[WorkerPreemptionResult] = []
        for interruption in self.source.list_active_interruptions():
            results.extend(self.preempt_interruption(interruption, now=current_time))
        return results

    def preempt_interruption(
        self,
        interruption: CapacityInterruption,
        *,
        now: datetime | None = None,
    ) -> list[WorkerPreemptionResult]:
        if interruption.state not in {
            AgentCapacityState.Preempting,
            AgentCapacityState.Cordoned,
        }:
            return []
        current_time = now or utc_now()
        results: list[WorkerPreemptionResult] = []
        for worker in self.workers.list_workers_on_machine(interruption.machine_id):
            if worker.pool != interruption.pool:
                continue
            operation = WorkerPreemptionOperation(
                operation_id=(
                    f"{interruption.enrollment_id}:"
                    f"{interruption.credential_generation}:"
                    f"{interruption.observed_at.isoformat()}"
                ),
                worker_id=worker.worker_id,
                capacity_owner_id=worker.capacity_owner_id,
                machine_id=interruption.machine_id,
                expected_resource_version=worker.resource_version,
                reason=interruption.reason,
                observed_at=interruption.observed_at,
            )
            results.append(self.preemption.preempt_worker(operation, now=current_time))
        return results


__all__ = [
    "CapacityInterruption",
    "CapacityInterruptionSource",
    "SchedulerCapacityInterruption",
    "SchedulerCapacityInterruptionService",
    "SchedulerWorkerPreemption",
    "SchedulerWorkerPreemptionService",
    "WorkerPreemptionContainerRepository",
    "WorkerPreemptionContainerStopper",
    "WorkerPreemptionOperation",
    "WorkerPreemptionQueueResult",
    "WorkerPreemptionRepository",
    "WorkerPreemptionResult",
]
