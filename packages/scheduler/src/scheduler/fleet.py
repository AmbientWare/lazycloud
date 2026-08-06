from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import Field
from shared.compute_policy import MachinePool
from shared.contracts import ContractModel
from shared.scheduling import SchedulerContainerStatus, SchedulerWorkerStatus
from shared.timestamps import utc_now

from scheduler.tools import WorkerPoolCapacity

DEFAULT_REQUEST_PROCESSING_INTERVAL = timedelta(seconds=1)
DEFAULT_MAX_SCHEDULE_RETRY_COUNT = 10
DEFAULT_MAX_SCHEDULE_RETRY_DURATION = timedelta(minutes=15)
DEFAULT_PROVISIONING_HANDOFF = timedelta(seconds=30)


class SchedulerMachineStatus(StrEnum):
    Pending = "pending"
    Registered = "registered"
    Ready = "ready"


class WorkerPoolStatus(StrEnum):
    Healthy = "healthy"
    Degraded = "degraded"


class SchedulerRequeueAction(StrEnum):
    Requeue = "requeue"
    Fail = "fail"


class SchedulerRetryReason(StrEnum):
    ScheduleFailed = "schedule-failed"
    WorkerCapacityWait = "worker-capacity-wait"
    WorkerProvisioningBackoff = "worker-provisioning-backoff"
    WorkerCapacityTimeout = "worker-capacity-timeout"
    RetryLimit = "retry-limit"
    NoController = "no-controller"
    RequeueFailed = "requeue-failed"


class SchedulerWorkerSnapshot(ContractModel):
    worker_id: str
    status: SchedulerWorkerStatus
    pool: MachinePool
    active_containers: list[str] = Field(default_factory=list)


class SchedulerContainerSnapshot(ContractModel):
    container_id: str
    status: SchedulerContainerStatus
    scheduled_at: datetime
    started_at: datetime | None = None


class SchedulerMachineSnapshot(ContractModel):
    machine_id: str
    status: SchedulerMachineStatus


class WorkerPoolStateSnapshot(ContractModel):
    capacity_owner_id: str = ""
    pool: MachinePool = MachinePool("")
    status: WorkerPoolStatus = WorkerPoolStatus.Healthy
    scheduling_latency_ms: int = 0
    pending_workers: int = 0
    available_workers: int = 0
    pending_containers: int = 0
    running_containers: int = 0
    free_cpu: float = 0
    free_memory_mib: int = 0
    free_gpu: int = 0
    registered_machines: int = 0
    pending_machines: int = 0
    ready_machines: int = 0


class SchedulerRequeuePlan(ContractModel):
    action: SchedulerRequeueAction
    reason: SchedulerRetryReason
    delay_seconds: float = 0
    next_retry_count: int = 0


def plan_pool_state(
    *,
    workers: list[SchedulerWorkerSnapshot],
    containers_by_worker: dict[str, list[SchedulerContainerSnapshot]],
    machines: list[SchedulerMachineSnapshot],
    free_capacity: WorkerPoolCapacity,
    now: datetime | None = None,
) -> WorkerPoolStateSnapshot:
    current = now or utc_now()
    latencies: list[timedelta] = []
    state = WorkerPoolStateSnapshot(
        free_cpu=free_capacity.free_cpu,
        free_memory_mib=free_capacity.free_memory_mib,
        free_gpu=free_capacity.free_gpu,
    )
    for machine in machines:
        if machine.status is SchedulerMachineStatus.Pending:
            state.pending_machines += 1
        elif machine.status is SchedulerMachineStatus.Registered:
            state.registered_machines += 1
        elif machine.status is SchedulerMachineStatus.Ready:
            state.ready_machines += 1
    for worker in workers:
        if worker.status is SchedulerWorkerStatus.Pending:
            state.pending_workers += 1
        elif worker.status is SchedulerWorkerStatus.Available:
            state.available_workers += 1
        for container in containers_by_worker.get(worker.worker_id, []):
            if container.status is SchedulerContainerStatus.Pending:
                state.pending_containers += 1
                latencies.append(current - container.scheduled_at)
            elif container.status is SchedulerContainerStatus.Running:
                state.running_containers += 1
                if container.started_at is not None:
                    latencies.append(container.started_at - container.scheduled_at)
    if latencies:
        total_ms = sum(max(0, int(latency.total_seconds() * 1000)) for latency in latencies)
        state.scheduling_latency_ms = total_ms // len(latencies)
    return state


def plan_worker_wait_requeue(
    *,
    request_created_at: datetime,
    now: datetime | None = None,
    delay: timedelta = DEFAULT_REQUEST_PROCESSING_INTERVAL,
    processing_interval: timedelta = DEFAULT_REQUEST_PROCESSING_INTERVAL,
    max_schedule_duration: timedelta = DEFAULT_MAX_SCHEDULE_RETRY_DURATION,
) -> SchedulerRequeuePlan:
    current = now or utc_now()
    if current - request_created_at >= max_schedule_duration:
        return SchedulerRequeuePlan(
            action=SchedulerRequeueAction.Fail,
            reason=SchedulerRetryReason.WorkerCapacityTimeout,
        )
    bounded_delay = max(delay, processing_interval)
    return SchedulerRequeuePlan(
        action=SchedulerRequeueAction.Requeue,
        reason=SchedulerRetryReason.WorkerCapacityWait,
        delay_seconds=bounded_delay.total_seconds(),
    )


def plan_retry_soon(
    *,
    retry_count: int,
    request_created_at: datetime,
    now: datetime | None = None,
    max_retry_count: int = DEFAULT_MAX_SCHEDULE_RETRY_COUNT,
    processing_interval: timedelta = DEFAULT_REQUEST_PROCESSING_INTERVAL,
    max_schedule_duration: timedelta = DEFAULT_MAX_SCHEDULE_RETRY_DURATION,
) -> SchedulerRequeuePlan:
    current = now or utc_now()
    if retry_count >= max_retry_count:
        return SchedulerRequeuePlan(
            action=SchedulerRequeueAction.Fail,
            reason=SchedulerRetryReason.RetryLimit,
            next_retry_count=retry_count,
        )
    if current - request_created_at >= max_schedule_duration:
        return SchedulerRequeuePlan(
            action=SchedulerRequeueAction.Fail,
            reason=SchedulerRetryReason.WorkerCapacityTimeout,
            next_retry_count=retry_count,
        )
    return SchedulerRequeuePlan(
        action=SchedulerRequeueAction.Requeue,
        reason=SchedulerRetryReason.ScheduleFailed,
        delay_seconds=processing_interval.total_seconds(),
        next_retry_count=retry_count + 1,
    )
