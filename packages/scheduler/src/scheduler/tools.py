from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import Field, JsonValue, model_validator
from shared.container_requests import capacity_memory_mib, fits_reservation
from shared.contracts import ContractModel
from shared.disks import DiskStorage
from shared.gpu import gpu_preference_accepts
from shared.placement import Placement, ProductRegion
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
    backfill: bool = False
    region: ProductRegion | None = None
    availability_zone: str = ""
    id: str
    owner_user_id: str = ""
    """Account that owns the requesting workspace, for attribution."""

    queue: str = "tasks"
    payload: JsonValue = None
    cpu: float = 1
    memory_mib: int = 512
    """The memory request; the container reserves `capacity_memory_mib` of it."""

    gpu: list[str] = Field(default_factory=list)
    """Models this request accepts, best first; empty asks for no GPU."""

    gpu_count: int = 0
    disk_bytes: int = 0
    """Declared size of the request's durable disks, reserved whole on a host-storage worker."""

    disk_count: int = 0
    """Durable disks the request mounts, one volume attachment each on a volume-storage worker."""

    placement: Placement
    required_worker_id: str = ""
    preferred_worker_id: str = ""
    """Chosen over other workers that fit, below liveness and above packing."""

    preferred_availability_zone: str = ""
    """Where a disk's cached volume is; ranks just below the preferred worker."""

    runtime_class: str = ""
    docker_enabled: bool = False
    preemptible: bool = False
    provisionable: bool = True
    retry_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)


def disk_claim(storage: DiskStorage, *, disk_bytes: int, disk_count: int) -> tuple[int, int]:
    """Bytes and volume attachments disks take on a worker with this storage.

    A host-storage worker holds each disk's declared size on its one
    filesystem. A volume-storage worker gives each disk a volume of its own, so
    what runs out is how many more the machine can attach.
    """
    if storage is DiskStorage.Volume:
        return 0, disk_count
    return disk_bytes, 0


class WorkerCapacity(ContractModel):
    region: ProductRegion | None = None
    availability_zone: str = ""
    worker_id: str
    machine_id: str = ""
    """Workers on one machine share its disk budget: host bytes or volume attachments."""

    placement: Placement
    """Where this worker is; a request lands here only when its placement is equal."""

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
    preemptible: bool = False
    free_cpu: float = Field(default=0, ge=0)
    free_memory_mib: int = Field(default=0, ge=0)
    free_gpu: int = Field(default=0, ge=0)
    free_disk_bytes: int = Field(default=0, ge=0)
    free_disk_volumes: int = Field(default=0, ge=0)
    total_cpu: float = Field(ge=0)
    total_memory_mib: int = Field(ge=0)
    total_gpu: int = Field(ge=0)
    total_disk_bytes: int = Field(default=0, ge=0)
    total_disk_volumes: int = Field(default=0, ge=0)
    disk_storage: DiskStorage = DiskStorage.Host
    pending: bool = False
    consolidating: bool = False
    """The reserve planner means to empty this machine, so it takes work last."""

    @model_validator(mode="after")
    def free_capacity_cannot_exceed_total_capacity(self) -> WorkerCapacity:
        if self.free_cpu > self.total_cpu:
            raise ValueError("free CPU cannot exceed total CPU")
        if self.free_memory_mib > self.total_memory_mib:
            raise ValueError("free memory cannot exceed total memory")
        if self.free_gpu > self.total_gpu:
            raise ValueError("free GPU count cannot exceed total GPU count")
        if self.free_disk_bytes > self.total_disk_bytes:
            raise ValueError("free disk bytes cannot exceed total disk bytes")
        if self.free_disk_volumes > self.total_disk_volumes:
            raise ValueError("free disk volumes cannot exceed total disk volumes")
        return self

    def disk_claim(self, request: SchedulingRequest) -> tuple[int, int]:
        return disk_claim(
            self.disk_storage, disk_bytes=request.disk_bytes, disk_count=request.disk_count
        )

    def disk_rejection(self, request: SchedulingRequest) -> str:
        disk_bytes, disk_volumes = self.disk_claim(request)
        if self.free_disk_bytes < disk_bytes:
            return f"free disk {self.free_disk_bytes} bytes < {disk_bytes} bytes"
        if self.free_disk_volumes < disk_volumes:
            return f"free disk volume attachments {self.free_disk_volumes} < {disk_volumes}"
        return ""

    def fit_rejection(self, request: SchedulingRequest) -> str:
        """Name the first reason this worker cannot take the request, else "".

        can_fit answers yes or no, so an unplaceable request produced a bare
        retry-limit with nothing describing the mismatch. Keep the checks in the
        same order as can_fit so the reported reason is the deciding one.
        """
        # Names no other placement: this reaches the requesting tenant as task error
        # text, and which other tenant's capacity this is is not theirs to learn.
        if request.placement != self.placement:
            return f"worker is not in placement {request.placement}"
        if request.required_worker_id and self.worker_id != request.required_worker_id:
            return "request requires the worker holding its source container"
        if request.region is not None and self.region != request.region:
            return "worker is outside the selected region"
        if request.availability_zone and self.availability_zone != request.availability_zone:
            return "worker is outside the selected availability zone"
        if request.runtime_class and request.runtime_class not in self.runtime_classes:
            return f"runtime {request.runtime_class!r} not in {self.runtime_classes}"
        if not request.preemptible and self.preemptible:
            return "worker is preemptible but request is not"
        if request.gpu_count > 0 and not self.total_gpu:
            return "request needs a GPU worker"
        if request.gpu_count == 0 and self.gpu_type and not request.backfill:
            return "worker is GPU-only"
        if self.free_cpu < request.cpu:
            return f"free cpu {self.free_cpu} < {request.cpu}"
        if not self.fits_resources(request):
            reserved = capacity_memory_mib(request.memory_mib)
            return f"free memory {self.free_memory_mib}MiB < {reserved}MiB reserved"
        if self.free_gpu < request.gpu_count:
            return f"free gpu {self.free_gpu} < {request.gpu_count}"
        return self.disk_rejection(request)

    def can_fit(self, request: SchedulingRequest) -> bool:
        if request.placement != self.placement:
            return False
        if request.required_worker_id and self.worker_id != request.required_worker_id:
            return False
        if request.region is not None and self.region != request.region:
            return False
        if request.availability_zone and self.availability_zone != request.availability_zone:
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
        elif self.gpu_type and (
            not request.backfill or not request.preemptible or self.free_gpu >= self.total_gpu
        ):
            return False
        return (
            self.fits_resources(request)
            and self.free_gpu >= request.gpu_count
            and not self.disk_rejection(request)
        )

    def fits_resources(self, request: SchedulingRequest) -> bool:
        return fits_reservation(
            _millicores(self.free_cpu),
            self.free_memory_mib,
            cpu_millicores=_millicores(request.cpu),
            memory_mib=request.memory_mib,
        )

    def reserve(self, request: SchedulingRequest) -> WorkerCapacity:
        disk_bytes, disk_volumes = self.disk_claim(request)
        return self.model_copy(
            update={
                "free_cpu": self.free_cpu - request.cpu,
                "free_memory_mib": self.free_memory_mib - capacity_memory_mib(request.memory_mib),
                "free_gpu": self.free_gpu - request.gpu_count,
                "free_disk_bytes": self.free_disk_bytes - disk_bytes,
                "free_disk_volumes": self.free_disk_volumes - disk_volumes,
            }
        )


def _millicores(cores: float) -> int:
    return round(cores * 1000)


class PlannedDispatch(ContractModel):
    worker_id: str
    request_id: str
    placement: Placement


def select_backfill_worker(
    request: SchedulingRequest,
    workers: Iterable[WorkerCapacity],
    queued_gpu_requests: Iterable[SchedulingRequest],
) -> WorkerCapacity | None:
    if request.gpu_count or request.gpu or not request.preemptible:
        return None
    pending_gpu = tuple(item for item in queued_gpu_requests if item.gpu_count > 0)
    candidates = [
        worker
        for worker in workers
        if worker.total_gpu > 0
        and worker.free_gpu < worker.total_gpu
        and not worker.pending
        and not any(gpu_request_matches_worker(item, worker) for item in pending_gpu)
    ]
    return select_worker_for_request(request.model_copy(update={"backfill": True}), candidates)


def gpu_request_matches_worker(request: SchedulingRequest, worker: WorkerCapacity) -> bool:
    if request.gpu_count <= 0:
        return False
    return worker.model_copy(
        update={
            "free_cpu": worker.total_cpu,
            "free_memory_mib": worker.total_memory_mib,
            "free_gpu": worker.total_gpu,
            "free_disk_bytes": worker.total_disk_bytes,
            "free_disk_volumes": worker.total_disk_volumes,
        }
    ).can_fit(request)


class WorkerCapacityReservation(ContractModel):
    worker_id: str
    request_id: str
    cpu: float
    memory_mib: int
    gpu_count: int = 0
    disk_bytes: int = 0
    disk_volumes: int = 0


class SchedulingOutcome(ContractModel):
    request_id: str
    decision: SchedulingDecision
    worker_id: str | None = None
    backfill: bool = False
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
    everyone = list(workers)
    candidates = [
        worker
        for worker in everyone
        if (include_pending or not worker.pending) and worker.can_fit(request)
    ]
    if not candidates:
        return None
    # Packing leaves machines empty, which is the only thing the idle
    # drain and the reserve planner can act on. Spreading reads as the safer
    # choice and keeps every machine touched, so none is ever idle long enough to
    # release and a pool settles at the machines it takes to keep them all warm.
    #
    # Priority tiers above packing and below liveness. A pending worker has not
    # registered yet, and preferring one leaves the request waiting beside a
    # worker that can run it now. The preferred worker holds local state such as
    # a disk's layers, and the preferred zone can reattach the disk's cached
    # volume, so both rank above packing, which would otherwise outweigh them.
    #
    # Busy machines take work before empty ones, the busiest first, so empty
    # machines stay empty for the drain. Among empty machines the smallest that
    # fits goes first, keeping large machines free for large requests. A machine
    # the planner means to consolidate takes work only when nothing else fits.
    #
    # Work that cannot be moved later is the exception in a quiet market: it goes
    # to the smallest machine, so the large ones it would otherwise pin can still
    # be released once they are idle.
    pin_small = not request.preemptible and _quiet(
        [
            worker
            for worker in everyone
            if worker.placement == request.placement
            and not worker.preemptible
            and bool(worker.gpu_type) == bool(request.gpu_count)
        ]
    )
    return min(
        candidates,
        key=lambda item: (
            item.pending,
            -item.priority,
            not request.preferred_worker_id or item.worker_id != request.preferred_worker_id,
            not request.preferred_availability_zone
            or item.availability_zone != request.preferred_availability_zone,
            item.consolidating,
            *(_smallest_first(item) if pin_small else ()),
            *_busiest_first(item),
            item.worker_id,
        ),
    )


def _reserved(worker: WorkerCapacity) -> tuple[float, int]:
    return worker.total_cpu - worker.free_cpu, worker.total_memory_mib - worker.free_memory_mib


def _smallest_first(worker: WorkerCapacity) -> tuple[float, ...]:
    return (worker.total_cpu, worker.total_memory_mib)


def _busiest_first(worker: WorkerCapacity) -> tuple[float, ...]:
    cpu, memory = _reserved(worker)
    if cpu > 0 or memory > 0:
        return (0, -cpu, -memory, -worker.total_cpu, -worker.total_memory_mib)
    return (1, 0, 0, worker.total_cpu, worker.total_memory_mib)


def _quiet(workers: list[WorkerCapacity]) -> bool:
    """Whether every candidate's load together would fit on the smallest of them."""
    if not workers:
        return True
    smallest = min(workers, key=_smallest_first)
    cpu = sum(_reserved(worker)[0] for worker in workers)
    memory = sum(_reserved(worker)[1] for worker in workers)
    return cpu <= smallest.total_cpu and memory <= smallest.total_memory_mib


def _share_disk_claim(
    remaining: dict[str, WorkerCapacity],
    worker: WorkerCapacity,
    *,
    disk_bytes: int,
    disk_volumes: int,
) -> None:
    """Take what a placement claimed from the other workers on its machine too."""
    if not (disk_bytes or disk_volumes) or not worker.machine_id:
        return
    for worker_id, sibling in remaining.items():
        if worker_id != worker.worker_id and sibling.machine_id == worker.machine_id:
            remaining[worker_id] = sibling.model_copy(
                update={
                    "free_disk_bytes": max(sibling.free_disk_bytes - disk_bytes, 0),
                    "free_disk_volumes": max(sibling.free_disk_volumes - disk_volumes, 0),
                }
            )


def plan_scheduling_batch(
    requests: Iterable[SchedulingRequest],
    workers: Iterable[WorkerCapacity],
    *,
    queued_gpu_requests: Iterable[SchedulingRequest],
    allow_provisioning: bool = True,
    worker_wait_delay: timedelta = timedelta(seconds=1),
) -> SchedulingBatchPlan:
    remaining = {worker.worker_id: worker.model_copy() for worker in workers}
    plan = SchedulingBatchPlan()
    # Oldest first. There is no per-request priority in the product, and the
    # unit's priority is a property of the capacity rather than of the work, so
    # it ranks workers below and not requests here.
    ordered_requests = sorted(requests, key=lambda item: (item.created_at, item.id))
    pending_gpu = tuple(queued_gpu_requests)
    for request in ordered_requests:
        worker = select_worker_for_request(request, remaining.values())
        backfill = False
        if worker is None:
            worker = select_backfill_worker(request, remaining.values(), pending_gpu)
            backfill = worker is not None
        if worker is not None:
            remaining[worker.worker_id] = worker.reserve(request)
            disk_bytes, disk_volumes = worker.disk_claim(request)
            _share_disk_claim(remaining, worker, disk_bytes=disk_bytes, disk_volumes=disk_volumes)
            plan.dispatches.append(
                PlannedDispatch(
                    worker_id=worker.worker_id,
                    request_id=request.id,
                    placement=worker.placement,
                )
            )
            plan.reservations.append(
                WorkerCapacityReservation(
                    worker_id=worker.worker_id,
                    request_id=request.id,
                    cpu=request.cpu,
                    memory_mib=capacity_memory_mib(request.memory_mib),
                    gpu_count=request.gpu_count,
                    disk_bytes=disk_bytes,
                    disk_volumes=disk_volumes,
                )
            )
            plan.outcomes.append(
                SchedulingOutcome(
                    request_id=request.id,
                    decision=SchedulingDecision.Dispatch,
                    worker_id=worker.worker_id,
                    backfill=backfill,
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
            disk_bytes, disk_volumes = pending_worker.disk_claim(request)
            _share_disk_claim(
                remaining, pending_worker, disk_bytes=disk_bytes, disk_volumes=disk_volumes
            )
            plan.reservations.append(
                WorkerCapacityReservation(
                    worker_id=pending_worker.worker_id,
                    request_id=request.id,
                    cpu=request.cpu,
                    memory_mib=capacity_memory_mib(request.memory_mib),
                    gpu_count=request.gpu_count,
                    disk_bytes=disk_bytes,
                    disk_volumes=disk_volumes,
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
