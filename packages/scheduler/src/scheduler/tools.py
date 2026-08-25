from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import Field, JsonValue, model_validator
from shared.compute_policy import LAZYCLOUD_MACHINE_POOL, MachinePool
from shared.contracts import ContractModel
from shared.gpu import gpu_preference_accepts
from shared.scheduling import worker_serves_owner
from shared.timestamps import utc_now


class SchedulingDecision(StrEnum):
    Dispatch = "dispatch"
    WaitForWorker = "wait-for-worker"
    ProvisionWorker = "provision-worker"
    RetryLater = "retry-later"
    Failed = "failed"


class WorkerPoolCapacity(ContractModel):
    free_cpu: float = 0
    pending_cpu: float = 0
    free_memory_mib: int = 0
    pending_memory_mib: int = 0
    free_gpu: int = 0
    pending_gpu: int = 0


class SchedulingRequest(ContractModel):
    id: str
    owner_user_id: str = ""
    """Account that owns the requesting workspace; what private placement compares."""

    queue: str = "tasks"
    payload: JsonValue = None
    cpu: float = 1
    memory_mib: int = 512
    gpu: list[str] = Field(default_factory=list)
    """Models this request accepts, best first; empty asks for no GPU."""

    gpu_count: int = 0
    pool_selector: str = ""
    runtime_class: str = ""
    docker_enabled: bool = False
    preemptible: bool = False
    provisionable: bool = True
    retry_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)


class WorkerCapacity(ContractModel):
    worker_id: str
    pool: MachinePool = MachinePool(LAZYCLOUD_MACHINE_POOL)
    owner_user_id: str = ""
    """Account whose machine this is; empty on the shared platform fleet."""

    private_worker: bool = False
    priority: int = 0
    """Feeding unit's preference for this worker, higher preferred.

    A tier above packing rather than a term mixed into it, so no value of it can
    stop capacity concentrating and no weight has to be justified.
    """

    gpu_type: str = ""
    runtime_class: str = ""
    runtime_classes: list[str] = Field(default_factory=list)
    requires_pool_selector: bool = False
    preemptible: bool = False
    free_cpu: float = Field(default=0, ge=0)
    free_memory_mib: int = Field(default=0, ge=0)
    free_gpu: int = Field(default=0, ge=0)
    total_cpu: float = Field(ge=0)
    total_memory_mib: int = Field(ge=0)
    total_gpu: int = Field(ge=0)
    pending: bool = False

    @model_validator(mode="after")
    def free_capacity_cannot_exceed_total_capacity(self) -> WorkerCapacity:
        if self.free_cpu > self.total_cpu:
            raise ValueError("free CPU cannot exceed total CPU")
        if self.free_memory_mib > self.total_memory_mib:
            raise ValueError("free memory cannot exceed total memory")
        if self.free_gpu > self.total_gpu:
            raise ValueError("free GPU count cannot exceed total GPU count")
        return self

    def serves_owner(self, owner_user_id: str) -> bool:
        return worker_serves_owner(
            private_worker=self.private_worker,
            worker_owner_user_id=self.owner_user_id,
            request_owner_user_id=owner_user_id,
        )

    def fit_rejection(self, request: SchedulingRequest) -> str:
        """Name the first reason this worker cannot take the request, else "".

        can_fit answers yes or no, so an unplaceable request produced a bare
        retry-limit with nothing describing the mismatch. Keep the checks in the
        same order as can_fit so the reported reason is the deciding one.
        """
        # Names no owner: this reaches the requesting tenant as task error text, and
        # which other tenant owns the worker is not theirs to learn.
        if not self.serves_owner(request.owner_user_id):
            return "worker is private to another account"
        if request.pool_selector and request.pool_selector != self.pool:
            return f"pool selector {request.pool_selector!r} != pool {self.pool!r}"
        if not request.pool_selector and self.requires_pool_selector:
            return "worker requires an explicit pool selector"
        if request.runtime_class and request.runtime_class not in self.runtime_classes:
            return f"runtime {request.runtime_class!r} not in {self.runtime_classes}"
        if not request.preemptible and self.preemptible:
            return "worker is preemptible but request is not"
        if request.gpu_count > 0 and not self.total_gpu:
            return "request needs a GPU worker"
        if request.gpu_count == 0 and self.gpu_type:
            return "worker is GPU-only"
        if self.free_cpu < request.cpu:
            return f"free cpu {self.free_cpu} < {request.cpu}"
        if self.free_memory_mib < request.memory_mib:
            return f"free memory {self.free_memory_mib}MiB < {request.memory_mib}MiB"
        if self.free_gpu < request.gpu_count:
            return f"free gpu {self.free_gpu} < {request.gpu_count}"
        return ""

    def can_fit(self, request: SchedulingRequest) -> bool:
        if not self.serves_owner(request.owner_user_id):
            return False
        if request.pool_selector and request.pool_selector != self.pool:
            return False
        if not request.pool_selector and self.requires_pool_selector:
            return False
        if request.runtime_class and request.runtime_class not in self.runtime_classes:
            return False
        if request.docker_enabled:
            if request.runtime_class:
                if request.runtime_class not in _DOCKER_ENABLED_RUNTIME_CLASSES:
                    return False
            elif not (set(self.runtime_classes) & _DOCKER_ENABLED_RUNTIME_CLASSES):
                return False
        if not request.preemptible and self.preemptible:
            return False
        if request.gpu_count > 0:
            if not gpu_preference_accepts(request.gpu, self.gpu_type):
                return False
        elif self.gpu_type:
            return False
        return (
            self.free_cpu >= request.cpu
            and self.free_memory_mib >= request.memory_mib
            and self.free_gpu >= request.gpu_count
        )

    def reserve(self, request: SchedulingRequest) -> WorkerCapacity:
        return self.model_copy(
            update={
                "free_cpu": self.free_cpu - request.cpu,
                "free_memory_mib": self.free_memory_mib - request.memory_mib,
                "free_gpu": self.free_gpu - request.gpu_count,
            }
        )


class PlannedDispatch(ContractModel):
    worker_id: str
    request_id: str
    pool: MachinePool


class WorkerCapacityReservation(ContractModel):
    worker_id: str
    request_id: str
    cpu: float
    memory_mib: int
    gpu_count: int = 0


class SchedulingOutcome(ContractModel):
    request_id: str
    decision: SchedulingDecision
    worker_id: str | None = None
    reason: str = ""
    requeue_delay_seconds: float = 0


class SchedulingBatchPlan(ContractModel):
    dispatches: list[PlannedDispatch] = Field(default_factory=list)
    reservations: list[WorkerCapacityReservation] = Field(default_factory=list)
    outcomes: list[SchedulingOutcome] = Field(default_factory=list)


_DOCKER_ENABLED_RUNTIME_CLASSES = frozenset({"runsc", "gvisor", "sandboxed-oci"})


def select_worker_for_request(
    request: SchedulingRequest,
    workers: Iterable[WorkerCapacity],
    *,
    include_pending: bool = False,
) -> WorkerCapacity | None:
    candidates = [
        worker
        for worker in workers
        if (include_pending or not worker.pending) and worker.can_fit(request)
    ]
    if not candidates:
        return None
    # The fullest worker that still fits, not the emptiest. Spreading reads as
    # the safer choice and quietly costs a floor under the pool: work lands on
    # whichever machine is least busy, so every machine keeps being touched,
    # none is ever idle long enough to release, and a pool settles at the number
    # of machines it takes to keep them all warm rather than the number the work
    # needs. Packing leaves machines genuinely empty, which is the only thing
    # the idle drain can act on.
    #
    # Priority tiers above that packing and below liveness. Above, because as a
    # tiebreak on a tuple of floats it would decide almost nothing and read as
    # implemented while doing nothing. Below `pending`, because a pending worker
    # has not registered yet: preferring one over a worker that can run the
    # request now leaves the request waiting beside capacity that was ready.
    # Within a tier the key is unchanged, so packing stays exact, and a tier
    # that receives nothing empties faster than it does today.
    return min(
        candidates,
        key=lambda item: (
            item.pending,
            -item.priority,
            *_post_placement_headroom(item, request),
            item.worker_id,
        ),
    )


def _post_placement_headroom(
    worker: WorkerCapacity,
    request: SchedulingRequest,
) -> tuple[float, ...]:
    headroom = [
        (worker.free_cpu - request.cpu) / worker.total_cpu if worker.total_cpu > 0 else 0.0,
        (worker.free_memory_mib - request.memory_mib) / worker.total_memory_mib
        if worker.total_memory_mib > 0
        else 0.0,
    ]
    if request.gpu_count > 0:
        headroom.append(
            (worker.free_gpu - request.gpu_count) / worker.total_gpu
            if worker.total_gpu > 0
            else 0.0
        )
    return tuple(sorted(headroom))


def plan_scheduling_batch(
    requests: Iterable[SchedulingRequest],
    workers: Iterable[WorkerCapacity],
    *,
    allow_provisioning: bool = True,
    worker_wait_delay: timedelta = timedelta(seconds=1),
) -> SchedulingBatchPlan:
    remaining = {worker.worker_id: worker.model_copy() for worker in workers}
    plan = SchedulingBatchPlan()
    # Oldest first. There is no per-request priority in the product, and the
    # unit's priority is a property of the capacity rather than of the work, so
    # it ranks workers below and not requests here.
    ordered_requests = sorted(requests, key=lambda item: (item.created_at, item.id))
    for request in ordered_requests:
        worker = select_worker_for_request(request, remaining.values())
        if worker is not None:
            remaining[worker.worker_id] = worker.reserve(request)
            plan.dispatches.append(
                PlannedDispatch(
                    worker_id=worker.worker_id,
                    request_id=request.id,
                    pool=worker.pool,
                )
            )
            plan.reservations.append(
                WorkerCapacityReservation(
                    worker_id=worker.worker_id,
                    request_id=request.id,
                    cpu=request.cpu,
                    memory_mib=request.memory_mib,
                    gpu_count=request.gpu_count,
                )
            )
            plan.outcomes.append(
                SchedulingOutcome(
                    request_id=request.id,
                    decision=SchedulingDecision.Dispatch,
                    worker_id=worker.worker_id,
                    reason="reserved existing worker capacity",
                )
            )
            continue

        pending_worker = select_worker_for_request(
            request,
            remaining.values(),
            include_pending=True,
        )
        if pending_worker is not None:
            remaining[pending_worker.worker_id] = pending_worker.reserve(request)
            plan.reservations.append(
                WorkerCapacityReservation(
                    worker_id=pending_worker.worker_id,
                    request_id=request.id,
                    cpu=request.cpu,
                    memory_mib=request.memory_mib,
                    gpu_count=request.gpu_count,
                )
            )
            plan.outcomes.append(
                SchedulingOutcome(
                    request_id=request.id,
                    decision=SchedulingDecision.WaitForWorker,
                    worker_id=pending_worker.worker_id,
                    reason="matching pending worker capacity exists",
                    requeue_delay_seconds=worker_wait_delay.total_seconds(),
                )
            )
            continue

        can_provision = allow_provisioning and request.provisionable
        decision = (
            SchedulingDecision.ProvisionWorker if can_provision else SchedulingDecision.Failed
        )
        plan.outcomes.append(
            SchedulingOutcome(
                request_id=request.id,
                decision=decision,
                reason="no worker capacity available",
                requeue_delay_seconds=worker_wait_delay.total_seconds() if can_provision else 0,
            )
        )
    return plan
