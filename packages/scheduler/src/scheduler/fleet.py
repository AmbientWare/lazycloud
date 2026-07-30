from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import Field, field_validator
from shared.app_identity import NAME
from shared.contracts import ContractModel
from shared.gpu import GpuType, normalize_gpu_type
from shared.scheduling import SchedulerContainerStatus, SchedulerWorkerStatus
from shared.timestamps import utc_now

from scheduler.tools import WorkerPoolCapacity

WORKER_LABEL_ID = f"{NAME}.io/worker-id"
WORKER_LABEL_POOL = f"{NAME}.io/pool"
WORKER_LABEL_MACHINE = f"{NAME}.io/machine-id"
DEFAULT_PENDING_WORKER_AGE_LIMIT = timedelta(minutes=15)
DEFAULT_REQUEST_PROCESSING_INTERVAL = timedelta(seconds=1)
DEFAULT_MAX_SCHEDULE_RETRY_COUNT = 10
DEFAULT_MAX_SCHEDULE_RETRY_DURATION = timedelta(minutes=15)
DEFAULT_PROVISIONING_HANDOFF = timedelta(seconds=30)


class WorkerPodPhase(StrEnum):
    Pending = "pending"
    Running = "running"
    Succeeded = "succeeded"
    Failed = "failed"
    Unknown = "unknown"


class SchedulerMachineStatus(StrEnum):
    Pending = "pending"
    Registered = "registered"
    Ready = "ready"


class SchedulerPoolMode(StrEnum):
    Local = "local"
    External = "external"
    Private = "private"


class WorkerPoolStatus(StrEnum):
    Healthy = "healthy"
    Degraded = "degraded"


class WorkerDeletedReason(StrEnum):
    PodCompleted = "pod-completed"
    PodWithoutState = "pod-without-state"
    PodExceededPendingAgeLimit = "pod-exceeded-pending-age-limit"
    WorkerStateWithoutJob = "worker-state-without-job"


class WorkerCleanupAction(StrEnum):
    Keep = "keep"
    DeleteJobAndState = "delete-job-and-state"
    DeleteState = "delete-state"


class PoolHealthEvent(StrEnum):
    None_ = "none"
    Degraded = "degraded"
    Healthy = "healthy"


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


class WorkerJobRef(ContractModel):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)


class WorkerPodRef(ContractModel):
    name: str
    job_name: str
    phase: WorkerPodPhase
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class WorkerStateRef(ContractModel):
    worker_id: str
    pool_name: str
    machine_id: str = ""
    active_container_ids: list[str] = Field(default_factory=list)


class WorkerCleanupDecision(ContractModel):
    action: WorkerCleanupAction
    worker_id: str = ""
    job_name: str = ""
    pod_name: str = ""
    pool_name: str = ""
    machine_id: str = ""
    reason: WorkerDeletedReason | None = None
    requeue_container_ids: list[str] = Field(default_factory=list)


class WorkerResourceCleanupPlan(ContractModel):
    decisions: list[WorkerCleanupDecision] = Field(default_factory=list)

    @property
    def destructive(self) -> list[WorkerCleanupDecision]:
        return [
            decision
            for decision in self.decisions
            if decision.action is not WorkerCleanupAction.Keep
        ]


class SchedulerWorkerSnapshot(ContractModel):
    worker_id: str
    status: SchedulerWorkerStatus
    pool_name: str
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
    pool_name: str = ""
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


class PoolFailoverConfig(ContractModel):
    enabled: bool = False
    max_pending_workers: int = 10
    max_scheduling_latency_ms: int = 60_000
    min_machines_available: int = 1

    @field_validator("max_pending_workers", "max_scheduling_latency_ms", "min_machines_available")
    @classmethod
    def failover_numbers_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "failover thresholds cannot be negative"
            raise ValueError(msg)
        return value


class PoolHealthTransitionPlan(ContractModel):
    previous_status: WorkerPoolStatus
    next_state: WorkerPoolStateSnapshot
    failover_reasons: list[str] = Field(default_factory=list)
    event: PoolHealthEvent = PoolHealthEvent.None_
    cordon_pending_workers: bool = False


class SchedulerRequeuePlan(ContractModel):
    action: SchedulerRequeueAction
    reason: SchedulerRetryReason
    delay_seconds: float = 0
    next_retry_count: int = 0


class ProvisioningReservationCompletionPlan(ContractModel):
    success: bool
    reservation_id: str
    release_immediately: bool
    release_after_seconds: float = 0
    record_failure: bool = False
    reason: str = ""


def worker_id_for_job_pod(job: WorkerJobRef, pod: WorkerPodRef) -> str:
    return pod.labels.get(WORKER_LABEL_ID) or job.labels.get(WORKER_LABEL_ID, "")


def machine_id_for_job_pod(job: WorkerJobRef, pod: WorkerPodRef) -> str:
    return pod.labels.get(WORKER_LABEL_MACHINE) or job.labels.get(WORKER_LABEL_MACHINE, "")


def pool_name_for_job_pod(job: WorkerJobRef, pod: WorkerPodRef) -> str:
    return pod.labels.get(WORKER_LABEL_POOL) or job.labels.get(WORKER_LABEL_POOL, "")


def plan_worker_pod_cleanup(
    job: WorkerJobRef,
    pod: WorkerPodRef,
    *,
    state: WorkerStateRef | None,
    now: datetime | None = None,
    pending_age_limit: timedelta = DEFAULT_PENDING_WORKER_AGE_LIMIT,
) -> WorkerCleanupDecision:
    worker_id = worker_id_for_job_pod(job, pod)
    machine_id = machine_id_for_job_pod(job, pod)
    pool_name = pool_name_for_job_pod(job, pod)
    if not worker_id:
        return WorkerCleanupDecision(
            action=WorkerCleanupAction.Keep,
            job_name=job.name,
            pod_name=pod.name,
            reason=None,
        )
    requeue_container_ids = state.active_container_ids if state else []
    if pod.phase in {WorkerPodPhase.Succeeded, WorkerPodPhase.Failed}:
        return WorkerCleanupDecision(
            action=WorkerCleanupAction.DeleteJobAndState,
            worker_id=worker_id,
            job_name=job.name,
            pod_name=pod.name,
            pool_name=pool_name,
            machine_id=machine_id,
            reason=WorkerDeletedReason.PodCompleted,
            requeue_container_ids=requeue_container_ids,
        )
    if state is None:
        return WorkerCleanupDecision(
            action=WorkerCleanupAction.DeleteJobAndState,
            worker_id=worker_id,
            job_name=job.name,
            pod_name=pod.name,
            pool_name=pool_name,
            machine_id=machine_id,
            reason=WorkerDeletedReason.PodWithoutState,
            requeue_container_ids=requeue_container_ids,
        )
    current = now or utc_now()
    if pod.phase is WorkerPodPhase.Pending and current - pod.created_at >= pending_age_limit:
        return WorkerCleanupDecision(
            action=WorkerCleanupAction.DeleteJobAndState,
            worker_id=worker_id,
            job_name=job.name,
            pod_name=pod.name,
            pool_name=pool_name,
            machine_id=machine_id,
            reason=WorkerDeletedReason.PodExceededPendingAgeLimit,
            requeue_container_ids=requeue_container_ids,
        )
    return WorkerCleanupDecision(
        action=WorkerCleanupAction.Keep,
        worker_id=worker_id,
        job_name=job.name,
        pod_name=pod.name,
        pool_name=pool_name,
        machine_id=machine_id,
        requeue_container_ids=requeue_container_ids,
    )


def plan_worker_state_without_job_cleanup(
    workers: list[WorkerStateRef],
    *,
    worker_job_ids: set[str],
    pool_name: str,
    machine_id: str = "",
) -> list[WorkerCleanupDecision]:
    decisions: list[WorkerCleanupDecision] = []
    for worker in workers:
        if worker.pool_name != pool_name:
            continue
        if machine_id and worker.machine_id != machine_id:
            continue
        if worker.worker_id in worker_job_ids:
            continue
        decisions.append(
            WorkerCleanupDecision(
                action=WorkerCleanupAction.DeleteState,
                worker_id=worker.worker_id,
                pool_name=worker.pool_name,
                machine_id=worker.machine_id,
                reason=WorkerDeletedReason.WorkerStateWithoutJob,
                requeue_container_ids=list(worker.active_container_ids),
            )
        )
    return decisions


def plan_worker_resource_cleanup(
    *,
    jobs: list[WorkerJobRef],
    pods: list[WorkerPodRef],
    worker_states: list[WorkerStateRef],
    pool_name: str,
    machine_id: str = "",
    now: datetime | None = None,
    pending_age_limit: timedelta = DEFAULT_PENDING_WORKER_AGE_LIMIT,
) -> WorkerResourceCleanupPlan:
    states_by_id = {state.worker_id: state for state in worker_states}
    pods_by_job: dict[str, list[WorkerPodRef]] = {}
    for pod in pods:
        pods_by_job.setdefault(pod.job_name, []).append(pod)
    decisions: list[WorkerCleanupDecision] = []
    worker_job_ids: set[str] = set()
    for job in jobs:
        if job.labels.get(WORKER_LABEL_POOL, "") != pool_name:
            continue
        for pod in pods_by_job.get(job.name, []):
            worker_id = worker_id_for_job_pod(job, pod)
            if worker_id:
                worker_job_ids.add(worker_id)
            decision = plan_worker_pod_cleanup(
                job,
                pod,
                state=states_by_id.get(worker_id),
                now=now,
                pending_age_limit=pending_age_limit,
            )
            decisions.append(decision)
    decisions.extend(
        plan_worker_state_without_job_cleanup(
            worker_states,
            worker_job_ids=worker_job_ids,
            pool_name=pool_name,
            machine_id=machine_id,
        )
    )
    return WorkerResourceCleanupPlan(decisions=decisions)


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


def plan_pool_health_transition(
    *,
    previous_status: WorkerPoolStatus,
    next_state: WorkerPoolStateSnapshot,
    mode: SchedulerPoolMode,
    failover: PoolFailoverConfig,
) -> PoolHealthTransitionPlan:
    state = next_state.model_copy(deep=True)
    status = WorkerPoolStatus.Healthy
    reasons: list[str] = []
    if (
        state.pending_workers >= failover.max_pending_workers
        and state.scheduling_latency_ms > failover.max_scheduling_latency_ms
    ):
        status = WorkerPoolStatus.Degraded
        reasons.append("exceeded max pending workers with high scheduling latency")
    if (
        mode is SchedulerPoolMode.External
        and state.ready_machines < failover.min_machines_available
    ):
        status = WorkerPoolStatus.Degraded
        reasons.append("not enough ready machines")
    state.status = status
    event = PoolHealthEvent.None_
    cordon = False
    if failover.enabled and previous_status != status:
        if status is WorkerPoolStatus.Degraded:
            event = PoolHealthEvent.Degraded
            cordon = True
        elif status is WorkerPoolStatus.Healthy:
            event = PoolHealthEvent.Healthy
    return PoolHealthTransitionPlan(
        previous_status=previous_status,
        next_state=state,
        failover_reasons=reasons,
        event=event,
        cordon_pending_workers=cordon,
    )


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


def plan_provisioning_reservation_completion(
    *,
    reservation_id: str,
    success: bool,
    handoff_delay: timedelta = DEFAULT_PROVISIONING_HANDOFF,
) -> ProvisioningReservationCompletionPlan:
    if success:
        return ProvisioningReservationCompletionPlan(
            success=True,
            reservation_id=reservation_id,
            release_immediately=False,
            release_after_seconds=handoff_delay.total_seconds(),
            reason="new worker receives reservation handoff window",
        )
    return ProvisioningReservationCompletionPlan(
        success=False,
        reservation_id=reservation_id,
        release_immediately=True,
        record_failure=True,
        reason="provisioning failed before worker handoff",
    )


def parse_scheduler_cpu_millicores(value: str | int) -> int:
    raw = str(value).strip()
    if raw.endswith("m"):
        raw = raw[:-1]
    try:
        parsed = int(raw)
    except ValueError as exc:
        msg = "invalid cpu value"
        raise ValueError(msg) from exc
    if parsed < 0:
        msg = "invalid cpu value"
        raise ValueError(msg)
    return parsed


def parse_scheduler_memory_mib(value: str | int) -> int:
    raw = str(value).strip()
    digits = ""
    suffix = ""
    for char in raw:
        if char.isdigit() and not suffix:
            digits += char
        else:
            suffix += char
    if not digits:
        msg = "invalid memory value"
        raise ValueError(msg)
    parsed = int(digits)
    if parsed < 0:
        msg = "invalid memory value"
        raise ValueError(msg)
    if suffix == "Ki":
        return parsed * 1024 // (1024 * 1024)
    if suffix in {"Mi", ""}:
        return parsed
    if suffix == "Gi":
        return parsed * 1024
    msg = "invalid memory unit"
    raise ValueError(msg)


def parse_scheduler_gpu_count(value: str | int) -> int:
    if isinstance(value, bool):
        msg = "invalid gpu count value"
        raise ValueError(msg)
    raw = str(value).strip()
    try:
        parsed = int(raw)
    except ValueError as exc:
        msg = "invalid gpu count value"
        raise ValueError(msg) from exc
    if str(parsed) != raw and not (raw.startswith("+") and str(parsed) == raw[1:]):
        msg = "invalid gpu count value"
        raise ValueError(msg)
    if parsed < 0:
        msg = "invalid gpu count value"
        raise ValueError(msg)
    return parsed


def parse_scheduler_gpu_type(value: str | int) -> GpuType:
    normalized = normalize_gpu_type(str(value))
    try:
        return GpuType(normalized)
    except ValueError as exc:
        msg = "invalid gpu type"
        raise ValueError(msg) from exc


def is_local_build_registry(registry: str) -> bool:
    if not registry:
        return False
    target = registry
    if "://" not in target:
        target = f"//{target}"
    parsed = urlparse(target)
    host = (parsed.hostname or registry.split(":", 1)[0]).lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")
