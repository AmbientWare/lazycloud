from __future__ import annotations

from collections.abc import Sequence
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
    SchedulerWorkerRequest,
    gpu_count_for_capacity,
)
from shared.timestamps import utc_now

from scheduler.worker_rollout import WorkerWorkloadDrainService


class WorkerPreemptionOperation(ContractModel):
    operation_id: str = Field(min_length=1, max_length=160)
    worker_id: str = Field(min_length=1, max_length=160)
    capacity_owner_id: str = Field(min_length=1, max_length=160)
    machine_id: str = Field(default="", max_length=160)
    expected_resource_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)
    observed_at: datetime


class WorkerPlannedDrainOperation(WorkerPreemptionOperation):
    """Fenced request to stop new placement without stopping started work."""


class WorkerPreemptionQueueResult(ContractModel):
    worker: SchedulerWorkerRecord
    changed: bool = False
    requeued_request_ids: list[str] = Field(default_factory=list)


class WorkerPreemptionResult(ContractModel):
    worker: SchedulerWorkerRecord
    changed: bool = False
    requeued_request_ids: list[str] = Field(default_factory=list)
    stopped_container_ids: list[str] = Field(default_factory=list)


class CapacityInterruption(ContractModel):
    enrollment_id: str
    credential_generation: int = Field(ge=1)
    workspace_id: str
    pool: MachinePool
    machine_id: str
    state: AgentCapacityState
    reason: str
    observed_at: datetime
    notice_at: datetime | None = None


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

    def drain_worker_for_maintenance(
        self,
        operation: WorkerPlannedDrainOperation,
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


class SchedulerWorkerMaintenance(Protocol):
    def drain_worker(
        self,
        operation: WorkerPlannedDrainOperation,
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


class GpuBackfillWorkerRepository(Protocol):
    def mark_gpu_backfill_evictions(
        self,
        worker: SchedulerWorkerRecord,
        gpu_request_id: str,
        container_ids: list[str],
        *,
        now: datetime | None = None,
    ) -> list[str]: ...


def select_gpu_backfill_victims(
    request: SchedulerWorkerRequest,
    worker: SchedulerWorkerRecord,
    containers: Sequence[SchedulerContainerState],
    *,
    reserved_memory_mib: int,
) -> list[str]:
    gpu_count = gpu_count_for_capacity(request.gpu, request.gpu_count)
    if gpu_count <= 0 or worker.free_gpu_count < gpu_count:
        return []
    needed_cpu = request.cpu_millicores - worker.free_cpu_millicores
    needed_memory = reserved_memory_mib - worker.free_memory_mib
    if needed_cpu <= 0 and needed_memory <= 0:
        return []
    victims: list[str] = []
    for container in sorted(containers, key=lambda item: item.scheduled_at, reverse=True):
        if (
            container.worker_id != worker.worker_id
            or not container.backfill
            or not container.preemptible
            or container.gpu_count != 0
            or container.status
            not in {SchedulerContainerStatus.Pending, SchedulerContainerStatus.Running}
        ):
            continue
        victims.append(container.container_id)
        needed_cpu -= container.cpu_millicores
        needed_memory -= container.memory_mib
        if needed_cpu <= 0 and needed_memory <= 0:
            return victims
    return []


@dataclass(slots=True)
class SchedulerGpuBackfillPreemptionService:
    workers: GpuBackfillWorkerRepository
    containers: WorkerPreemptionContainerRepository
    stopper: WorkerPreemptionContainerStopper

    def recover(
        self,
        request: SchedulerWorkerRequest,
        worker: SchedulerWorkerRecord,
        *,
        reserved_memory_mib: int,
    ) -> bool:
        victims = select_gpu_backfill_victims(
            request,
            worker,
            self.containers.list_by_worker(worker.worker_id),
            reserved_memory_mib=reserved_memory_mib,
        )
        if not victims:
            return False
        marked = self.workers.mark_gpu_backfill_evictions(worker, request.container_id, victims)
        for container_id in marked:
            self.stopper.stop(container_id, reason=StopContainerReason.Preempted)
        return True


@dataclass(slots=True)
class SchedulerWorkerMaintenanceService:
    workers: WorkerPreemptionRepository

    def drain_worker(
        self,
        operation: WorkerPlannedDrainOperation,
        *,
        now: datetime | None = None,
    ) -> WorkerPreemptionResult:
        queued = self.workers.drain_worker_for_maintenance(
            operation,
            now=now or utc_now(),
        )
        return WorkerPreemptionResult(
            worker=queued.worker,
            changed=queued.changed,
            requeued_request_ids=queued.requeued_request_ids,
        )


@dataclass(slots=True)
class SchedulerCapacityInterruptionService:
    preemption: SchedulerWorkerPreemption
    workers: WorkerPreemptionRepository
    source: CapacityInterruptionSource | None = None
    maintenance: SchedulerWorkerMaintenance | None = None
    workload_drains: WorkerWorkloadDrainService | None = None

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
            AgentCapacityState.Draining,
            AgentCapacityState.Preempting,
            AgentCapacityState.Cordoned,
        }:
            return []
        current_time = now or utc_now()
        draining = interruption.state is AgentCapacityState.Draining and (
            interruption.notice_at is None or current_time < interruption.notice_at
        )
        results: list[WorkerPreemptionResult] = []
        for worker in self.workers.list_workers_on_machine(interruption.machine_id):
            if worker.pool != interruption.pool:
                continue
            operation = WorkerPreemptionOperation(
                operation_id=(
                    f"{interruption.enrollment_id}:"
                    f"{interruption.credential_generation}:"
                    f"{interruption.observed_at.isoformat()}:"
                    f"{'drain' if draining else 'preempt'}"
                ),
                worker_id=worker.worker_id,
                capacity_owner_id=worker.capacity_owner_id,
                machine_id=interruption.machine_id,
                expected_resource_version=worker.resource_version,
                reason=interruption.reason,
                observed_at=interruption.observed_at,
            )
            if draining:
                if self.maintenance is None:
                    raise RuntimeError("planned worker maintenance service is not configured")
                results.append(
                    self.maintenance.drain_worker(
                        WorkerPlannedDrainOperation.model_validate(operation.model_dump()),
                        now=current_time,
                    )
                )
                if interruption.notice_at is not None:
                    if self.workload_drains is None:
                        raise RuntimeError("capacity interruption workload drain is not configured")
                    self.workload_drains.prepare(
                        worker.worker_id, now=current_time, close_admission=True
                    )
            else:
                results.append(self.preemption.preempt_worker(operation, now=current_time))
        return results


__all__ = [
    "CapacityInterruption",
    "CapacityInterruptionSource",
    "SchedulerCapacityInterruption",
    "SchedulerCapacityInterruptionService",
    "SchedulerWorkerMaintenance",
    "SchedulerWorkerMaintenanceService",
    "SchedulerWorkerPreemption",
    "SchedulerWorkerPreemptionService",
    "WorkerPlannedDrainOperation",
    "WorkerPreemptionContainerRepository",
    "WorkerPreemptionContainerStopper",
    "WorkerPreemptionOperation",
    "WorkerPreemptionQueueResult",
    "WorkerPreemptionRepository",
    "WorkerPreemptionResult",
]
