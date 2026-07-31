from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from secrets import token_urlsafe
from threading import Event, Thread
from typing import Protocol
from uuid import uuid4

from coordination.redis_client import RedisClient, redis_text
from coordination.token_lock import release_token_lock, try_acquire_token_lock
from pydantic import Field, model_validator
from shared.capacity import (
    CapacityAcquisitionPlanningRequest as ComputeCapacityPlanningRequest,
)
from shared.capacity import CapacityAcquisitionRequest as ComputeCapacityRequest
from shared.capacity import CapacityAcquisitionResult as ComputeCapacityResult
from shared.capacity import CapacityAcquisitionShape as ComputeCapacityShape
from shared.capacity import CapacityAcquisitionStatus as ComputeCapacityStatus
from shared.capacity import CapacityOwnerKind, CapacityPoolSizingState
from shared.capacity import CapacityReleaseRequest as ComputeCapacityReleaseRequest
from shared.compute_fleet import Pool
from shared.compute_policy import ComputePlacementSource
from shared.contracts import ContractModel
from shared.errors import ConflictError
from shared.scheduling import (
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    SchedulerWorkerStatus,
    gpu_count_for_capacity,
)
from shared.timestamps import utc_now

from scheduler.pool_sizing import (
    CapacityPoolOperationalHealth,
    CapacityPoolSizingStateService,
    WorkerPoolSizingAction,
    WorkerPoolSizingAllocation,
    WorkerPoolSizingPlan,
    WorkerPoolSizingReservation,
    capacity_pool_operational_health,
    capacity_pool_selection_key,
    effective_pool_headroom,
    plan_worker_pool_sizing,
    sizing_failure_state,
    sizing_state_update,
)
from scheduler.state import (
    CapacityReservationDispatchAllocation,
    WorkerReservedCapacity,
    capacity_memory_mib,
    capacity_owner_key_segment,
)

DEFAULT_CAPACITY_MUTATION_LOCK_SECONDS = 300
DEFAULT_CAPACITY_RESERVATION_RETENTION_SECONDS = 86_400

RENEW_CAPACITY_MUTATION_LOCK_SCRIPT = """
if redis.call("GET", KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call("EXPIRE", KEYS[1], ARGV[2])
return 1
"""


class CapacityReservationStatus(StrEnum):
    Reserved = "reserved"
    Provisioning = "provisioning"
    Registered = "registered"
    Failed = "failed"
    Expired = "expired"
    Released = "released"


class CapacityReservationSource(StrEnum):
    PendingWorker = "pending_worker"
    PlacementMiss = "placement_miss"


class CapacityAcquisitionStatus(StrEnum):
    ExistingPending = "existing_pending"
    Requested = "requested"
    AtLimit = "at_limit"
    TemporarilyUnavailable = "temporarily_unavailable"
    Unsupported = "unsupported"


class CapacityRequestShape(ContractModel):
    cpu_millicores: int = Field(ge=0)
    memory_mib: int = Field(ge=0)
    gpu_type: str = ""
    gpu_count: int = Field(default=0, ge=0)
    runtime_class: str = ""
    runtime_classes: tuple[str, ...] = ("runc",)
    docker_enabled: bool = False
    preemptible: bool = False

    @model_validator(mode="after")
    def validate_gpu_shape(self) -> CapacityRequestShape:
        if (self.gpu_type == "") != (self.gpu_count == 0):
            raise ValueError("capacity reservation GPU type and count must be configured together")
        return self

    def can_host(self, request: SchedulerWorkerRequest) -> bool:
        requested_gpu = gpu_count_for_capacity(
            request.gpu_type,
            request.gpu_request,
            request.gpu_count,
        )
        if self.cpu_millicores < request.cpu_millicores:
            return False
        if self.memory_mib < capacity_memory_mib(request.memory_mib):
            return False
        if self.gpu_count < requested_gpu:
            return False
        if requested_gpu <= 0 and self.gpu_count > 0:
            return False
        if requested_gpu > 0 and not _gpu_matches(self.gpu_type, request):
            return False
        if request.runtime_class and request.runtime_class not in self.runtime_classes:
            return False
        if request.docker_enabled and not _supports_docker(self.runtime_classes):
            return False
        return request.preemptible or not self.preemptible

    def worker_capabilities_match(self, worker: SchedulerWorkerRecord) -> bool:
        return (
            worker.total_gpu_count >= self.gpu_count
            and (self.gpu_count == 0 or worker.gpu_type == self.gpu_type)
            and all(runtime in worker.runtime_classes for runtime in self.runtime_classes)
            and worker.preemptible is self.preemptible
        )


class CapacityReservationAllocation(ContractModel):
    reservation_id: str
    container_id: str
    workspace_id: str
    cpu_millicores: int = Field(ge=0)
    memory_mib: int = Field(ge=0)
    gpu_count: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)


class CapacityProvisioningReservation(ContractModel):
    id: str
    resource_version: int = Field(default=0, ge=0)
    capacity_owner_id: str
    pool_name: str
    owner_kind: CapacityOwnerKind
    source: CapacityReservationSource
    status: CapacityReservationStatus = CapacityReservationStatus.Reserved
    acquisition_shape: CapacityRequestShape
    schedulable_shape: CapacityRequestShape | None = None
    operation_id: str
    target_worker_id: str = ""
    target_machine_id: str = ""
    desired_unit: int = Field(default=0, ge=0)
    acquisition_created: bool = False
    release_requested: bool = False
    release_target_unit: int | None = Field(default=None, ge=0)
    registration_deadline_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    terminal_reason: str = ""

    @property
    def open(self) -> bool:
        return self.status in {
            CapacityReservationStatus.Reserved,
            CapacityReservationStatus.Provisioning,
            CapacityReservationStatus.Registered,
        }

    @property
    def accepting_allocations(self) -> bool:
        return self.open and not self.release_requested

    @property
    def allocation_shape(self) -> CapacityRequestShape:
        return self.schedulable_shape or self.acquisition_shape


class CapacityAcquisitionResult(ContractModel):
    status: CapacityAcquisitionStatus
    capacity_owner_id: str
    reservation_id: str
    operation_id: str
    desired_unit: int = Field(default=0, ge=0)
    target_worker_id: str = ""
    target_machine_id: str = ""
    retry_delay_seconds: float = Field(default=1.0, ge=0)
    reason: str = ""


class CapacityReservationDecision(ContractModel):
    reservation: CapacityProvisioningReservation
    allocation: CapacityReservationAllocation
    created: bool = False


class CapacityReservationConflictError(ConflictError):
    """Capacity owner state changed while one scheduler operation was in flight."""


class CapacityReservationLockContendedError(CapacityReservationConflictError):
    """Another reconciler currently owns the capacity-owner mutation lease."""


class CapacityReservationLeaseLostError(CapacityReservationConflictError):
    """The capacity-owner mutation lease could not be renewed or was replaced."""


class CapacityReservationVersionConflictError(CapacityReservationConflictError):
    """A reservation resource version or state transition was superseded."""


class CapacityReservationStateTransitionError(CapacityReservationConflictError):
    """A reservation attempted to leave the frozen lifecycle graph."""


class PendingCapacityOwner(ContractModel):
    capacity_owner_id: str
    owner_kind: CapacityOwnerKind
    pool_name: str
    workspace_id: str = ""
    registration_timeout_seconds: int = Field(default=600, ge=30, le=3_600)

    def accepts(
        self,
        request: SchedulerWorkerRequest,
        worker: SchedulerWorkerRecord,
    ) -> bool:
        return (
            worker.capacity_owner_id == self.capacity_owner_id
            and worker.pool_name == self.pool_name
            and (not self.workspace_id or request.workspace_id == self.workspace_id)
            and (
                request.capacity_owner_id in {"", self.capacity_owner_id}
                and request.pool_selector in {"", self.pool_name}
            )
        )


class CapacityAcquisitionController(Protocol):
    @property
    def capacity_owner_id(self) -> str: ...

    @property
    def owner_kind(self) -> CapacityOwnerKind: ...

    @property
    def pool_name(self) -> str: ...

    @property
    def registration_timeout(self) -> timedelta: ...

    @property
    def priority(self) -> int: ...

    def accepts(self, request: SchedulerWorkerRequest) -> bool: ...

    def operational_health(
        self,
        *,
        now: datetime,
    ) -> CapacityPoolOperationalHealth: ...

    def reconcile_sizing(
        self,
        *,
        reservations: Sequence[WorkerPoolSizingReservation],
        allocations: Sequence[WorkerPoolSizingAllocation],
        now: datetime,
    ) -> WorkerPoolSizingPlan: ...

    def reservation_shape(self, request: SchedulerWorkerRequest) -> CapacityRequestShape: ...

    def plan_acquisition(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        owner_reservations: tuple[CapacityProvisioningReservation, ...],
        now: datetime,
    ) -> CapacityAcquisitionResult: ...

    def ensure_acquisition(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        now: datetime,
    ) -> CapacityAcquisitionResult: ...

    def reconcile(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        owner_reservations: tuple[CapacityProvisioningReservation, ...],
        now: datetime,
    ) -> CapacityAcquisitionResult: ...

    def plan_release(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        owner_reservations: tuple[CapacityProvisioningReservation, ...],
        now: datetime,
    ) -> CapacityAcquisitionResult: ...

    def release(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        owner_reservations: tuple[CapacityProvisioningReservation, ...],
        now: datetime,
    ) -> CapacityAcquisitionResult: ...


class CapacityAllocationOwnerDirectory(Protocol):
    def is_active(self, *, workspace_id: str, container_id: str) -> bool: ...


class CapacityWorkerRepository(Protocol):
    def list_workers(self) -> list[SchedulerWorkerRecord]: ...


class ComputeCapacityService(CapacityPoolSizingStateService, Protocol):
    def plan_capacity_acquisition(
        self,
        request: ComputeCapacityPlanningRequest,
    ) -> ComputeCapacityResult: ...

    def acquire_capacity(self, request: ComputeCapacityRequest) -> ComputeCapacityResult: ...

    def release_acquired_capacity(
        self,
        request: ComputeCapacityReleaseRequest,
    ) -> ComputeCapacityResult: ...


@dataclass(slots=True)
class ComputePoolCapacityController:
    workspace_id: str
    pool: Pool
    compute: ComputeCapacityService
    workers: CapacityWorkerRepository

    @property
    def capacity_owner_id(self) -> str:
        return self.pool.capacity_owner_id

    @property
    def owner_kind(self) -> CapacityOwnerKind:
        return self.pool.capacity_owner_kind

    @property
    def pool_name(self) -> str:
        return self.pool.name

    @property
    def registration_timeout(self) -> timedelta:
        return timedelta(seconds=self.pool.registration_timeout_seconds)

    @property
    def priority(self) -> int:
        return self.pool.priority

    def operational_health(
        self,
        *,
        now: datetime,
    ) -> CapacityPoolOperationalHealth:
        return capacity_pool_operational_health(
            self.capacity_owner_id,
            self.workers.list_workers(),
            state=self.compute.get_pool_sizing_state(self.capacity_owner_id),
            now=now,
        )

    def accepts(self, request: SchedulerWorkerRequest) -> bool:
        if not self.pool.scaling_enabled:
            return False
        if self.owner_kind not in {
            CapacityOwnerKind.ManagedPool,
            CapacityOwnerKind.PooledProvider,
        }:
            return False
        if request.workspace_id != self.workspace_id:
            return False
        if request.capacity_owner_id and request.capacity_owner_id != self.capacity_owner_id:
            return False
        if request.capacity_owner_id:
            if request.pool_selector and request.pool_selector != self.pool.name:
                return False
        elif _request_has_strict_pool(request):
            if request.pool_selector != self.pool.name:
                return False
        elif not self.pool.default_eligible:
            return False
        return self.reservation_shape(request).can_host(request)

    def reservation_shape(self, request: SchedulerWorkerRequest) -> CapacityRequestShape:
        return CapacityRequestShape(
            cpu_millicores=self.pool.worker_cpu_millicores,
            memory_mib=self.pool.worker_memory_mib,
            gpu_type=self.pool.worker_gpu_type,
            gpu_count=self.pool.worker_gpu_count,
            runtime_class=request.runtime_class or self.pool.worker_runtimes[0],
            runtime_classes=self.pool.worker_runtimes,
            docker_enabled=request.docker_enabled,
            preemptible=self.pool.worker_preemptible,
        )

    def reconcile_sizing(
        self,
        *,
        reservations: Sequence[WorkerPoolSizingReservation],
        allocations: Sequence[WorkerPoolSizingAllocation],
        now: datetime,
    ) -> WorkerPoolSizingPlan:
        workers = self.workers.list_workers()
        headroom = effective_pool_headroom(
            self.pool,
            workers,
            reservations=reservations,
            allocations=allocations,
        )
        registered_units = headroom.available_workers + headroom.unclaimed_pending_workers
        state = _sizing_state(self.pool, self.compute, registered_units)
        authoritative_units = max(
            registered_units,
            state.target_units if state.operation_id else 0,
            *(reservation.desired_unit for reservation in reservations),
        )
        plan = plan_worker_pool_sizing(
            self.pool,
            headroom=headroom,
            registered_units=registered_units,
            authoritative_units=authoritative_units,
            state=state,
            now=now,
        )
        state = _record_sizing_observation(self.compute, state, plan)
        if state.operation_id and registered_units < state.target_units:
            if state.retry_after_at is not None and state.retry_after_at > now:
                return plan
            return self._ensure_sizing_operation(state, plan, now=now)
        if plan.action is not WorkerPoolSizingAction.ScaleUp:
            return plan
        operation_id = str(uuid4())
        state = _save_sizing_state(
            self.compute,
            state.model_copy(
                update={
                    "operation_id": operation_id,
                    "target_units": plan.target_units,
                    "operation_started_at": now,
                }
            ),
        )
        return self._ensure_sizing_operation(state, plan, now=now)

    def _ensure_sizing_operation(
        self,
        state: CapacityPoolSizingState,
        plan: WorkerPoolSizingPlan,
        *,
        now: datetime,
    ) -> WorkerPoolSizingPlan:
        shape = ComputeCapacityShape(
            cpu_millicores=self.pool.worker_cpu_millicores,
            memory_mib=self.pool.worker_memory_mib,
            gpu_type=self.pool.worker_gpu_type,
            gpu_count=self.pool.worker_gpu_count,
            runtime=self.pool.worker_runtimes[0],
            preemptible=self.pool.worker_preemptible,
        )
        try:
            planned = self.compute.plan_capacity_acquisition(
                ComputeCapacityPlanningRequest(
                    capacity_owner_id=self.capacity_owner_id,
                    reservation_id=state.operation_id,
                    operation_id=state.operation_id,
                    shape=shape,
                )
            )
            target_units = max(planned.desired_unit, state.target_units)
            state = _save_sizing_state(
                self.compute, state.model_copy(update={"target_units": target_units})
            )
            result = (
                self.compute.acquire_capacity(
                    ComputeCapacityRequest(
                        capacity_owner_id=self.capacity_owner_id,
                        reservation_id=state.operation_id,
                        operation_id=state.operation_id,
                        desired_unit=target_units,
                        shape=shape,
                    )
                )
                if planned.status is ComputeCapacityStatus.Requested
                else planned
            )
        except Exception:
            _save_sizing_state(
                self.compute,
                sizing_failure_state(state, self.pool, now=now).model_copy(
                    update={"terminal_reason": "compute pool sizing operation failed"}
                ),
            )
            return plan.model_copy(
                update={
                    "action": WorkerPoolSizingAction.Wait,
                    "reason": "compute pool sizing operation failed and entered durable backoff",
                }
            )
        if result.status in {
            ComputeCapacityStatus.TemporarilyUnavailable,
            ComputeCapacityStatus.Unsupported,
        }:
            _save_sizing_state(
                self.compute,
                sizing_failure_state(state, self.pool, now=now).model_copy(
                    update={"terminal_reason": result.reason}
                ),
            )
            return plan.model_copy(
                update={
                    "action": WorkerPoolSizingAction.Wait,
                    "reason": result.reason or "compute pool sizing is temporarily unavailable",
                }
            )
        if result.status is ComputeCapacityStatus.AtLimit:
            _save_sizing_state(
                self.compute,
                state.model_copy(
                    update={
                        "operation_id": "",
                        "operation_started_at": None,
                        "target_units": result.desired_unit,
                        "terminal_reason": result.reason or "compute pool capacity is at limit",
                    }
                ),
            )
            return plan.model_copy(
                update={
                    "action": WorkerPoolSizingAction.None_,
                    "target_units": result.desired_unit,
                    "reason": result.reason or "compute pool capacity is at limit",
                }
            )
        _save_sizing_state(
            self.compute,
            state.model_copy(
                update={
                    "target_units": result.desired_unit,
                    "last_scale_up_at": (
                        now
                        if result.status is ComputeCapacityStatus.Requested
                        else state.last_scale_up_at
                    ),
                    "retry_after_at": None,
                    "consecutive_failures": 0,
                    "terminal_reason": "",
                }
            ),
        )
        return plan.model_copy(update={"target_units": result.desired_unit})

    def plan_acquisition(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        owner_reservations: tuple[CapacityProvisioningReservation, ...],
        now: datetime,
    ) -> CapacityAcquisitionResult:
        _ = owner_reservations, now
        result = self.compute.plan_capacity_acquisition(
            ComputeCapacityPlanningRequest(
                capacity_owner_id=reservation.capacity_owner_id,
                reservation_id=reservation.id,
                operation_id=reservation.operation_id,
                shape=_compute_capacity_shape(reservation.acquisition_shape),
            )
        )
        return _compute_acquisition_result(reservation, result)

    def ensure_acquisition(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        now: datetime,
    ) -> CapacityAcquisitionResult:
        _ = now
        if reservation.desired_unit <= 0:
            raise ValueError("compute capacity acquisition requires a persisted desired unit")
        result = self.compute.acquire_capacity(
            ComputeCapacityRequest(
                capacity_owner_id=reservation.capacity_owner_id,
                reservation_id=reservation.id,
                operation_id=reservation.operation_id,
                desired_unit=reservation.desired_unit,
                shape=_compute_capacity_shape(reservation.acquisition_shape),
            )
        )
        return _compute_acquisition_result(reservation, result)

    def reconcile(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        owner_reservations: tuple[CapacityProvisioningReservation, ...],
        now: datetime,
    ) -> CapacityAcquisitionResult:
        _ = owner_reservations
        return self.ensure_acquisition(reservation, now=now)

    def plan_release(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        owner_reservations: tuple[CapacityProvisioningReservation, ...],
        now: datetime,
    ) -> CapacityAcquisitionResult:
        _ = owner_reservations, now
        return CapacityAcquisitionResult(
            status=CapacityAcquisitionStatus.ExistingPending,
            capacity_owner_id=reservation.capacity_owner_id,
            reservation_id=reservation.id,
            operation_id=reservation.operation_id,
            desired_unit=reservation.desired_unit,
            target_machine_id=reservation.target_machine_id,
            reason="provider operation owns an exact independently releasable unit",
        )

    def release(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        owner_reservations: tuple[CapacityProvisioningReservation, ...],
        now: datetime,
    ) -> CapacityAcquisitionResult:
        _ = owner_reservations, now
        if not reservation.acquisition_created:
            return CapacityAcquisitionResult(
                status=CapacityAcquisitionStatus.ExistingPending,
                capacity_owner_id=reservation.capacity_owner_id,
                reservation_id=reservation.id,
                operation_id=reservation.operation_id,
                desired_unit=reservation.desired_unit,
                reason="reservation reused existing capacity and owns no unit to release",
            )
        result = self.compute.release_acquired_capacity(
            ComputeCapacityReleaseRequest(
                capacity_owner_id=reservation.capacity_owner_id,
                reservation_id=reservation.id,
                operation_id=reservation.operation_id,
            )
        )
        return _compute_acquisition_result(reservation, result)


@dataclass(frozen=True, slots=True)
class CapacityReservationKeys:
    redis: RedisClient
    namespace: str = "scheduler"

    def reservation_index(self) -> str:
        return self.redis.key(self.namespace, "capacity-reservations", "index")

    def reservation(self, reservation_id: str) -> str:
        return self.redis.key(self.namespace, "capacity-reservations", reservation_id, "state")

    def allocations(self, reservation_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "capacity-reservations",
            reservation_id,
            "allocations",
        )

    def allocation(self, reservation_id: str, container_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "capacity-reservations",
            reservation_id,
            "allocations",
            container_id,
        )

    def request_reservation(self, container_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "capacity-reservation-requests",
            container_id,
        )

    def owner_reservations(self, capacity_owner_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "capacity-owners",
            _owner_key(capacity_owner_id),
            "reservations",
        )

    def mutation_lock(self, capacity_owner_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "capacity-owners",
            _owner_key(capacity_owner_id),
            "mutation-lock",
        )


@dataclass(slots=True)
class RedisCapacityReservationRepository:
    redis: RedisClient
    keys: CapacityReservationKeys

    def __init__(
        self,
        redis: RedisClient,
        keys: CapacityReservationKeys | None = None,
    ) -> None:
        self.redis = redis
        self.keys = keys or CapacityReservationKeys(redis)

    @contextmanager
    def mutation_lock(
        self,
        capacity_owner_id: str,
        *,
        ttl_seconds: int = DEFAULT_CAPACITY_MUTATION_LOCK_SECONDS,
    ) -> Iterator[None]:
        key = self.keys.mutation_lock(capacity_owner_id)
        token = token_urlsafe(24)
        if not try_acquire_token_lock(self.redis, key, token, ttl_seconds=ttl_seconds):
            raise CapacityReservationLockContendedError(
                f"capacity owner {capacity_owner_id} is already being reconciled"
            )
        stop_renewal = Event()
        lease_lost = Event()

        def renew() -> None:
            interval_seconds = max(ttl_seconds / 3, 0.1)
            while not stop_renewal.wait(interval_seconds):
                try:
                    renewed = self.redis.eval_int(
                        RENEW_CAPACITY_MUTATION_LOCK_SCRIPT,
                        1,
                        key,
                        token,
                        ttl_seconds,
                    )
                except Exception:
                    lease_lost.set()
                    return
                if renewed != 1:
                    lease_lost.set()
                    return

        renewal = Thread(
            target=renew,
            name=f"capacity-lock-{capacity_owner_key_segment(capacity_owner_id)[:12]}",
            daemon=True,
        )
        renewal.start()
        body_failed = False
        try:
            yield
        except BaseException:
            body_failed = True
            raise
        finally:
            stop_renewal.set()
            renewal.join(timeout=max(min(ttl_seconds / 3, 1.0), 0.1))
            try:
                current_token = self.redis.get(key)
            except Exception:
                lease_lost.set()
            else:
                if current_token is None or redis_text(current_token) != token:
                    lease_lost.set()
            try:
                release_token_lock(self.redis, key, token)
            except Exception:
                lease_lost.set()
            if lease_lost.is_set() and not body_failed:
                raise CapacityReservationLeaseLostError(
                    f"capacity owner {capacity_owner_id} mutation lease was lost"
                )

    def reserve(
        self,
        *,
        capacity_owner_id: str,
        pool_name: str,
        owner_kind: CapacityOwnerKind,
        request: SchedulerWorkerRequest,
        shape: CapacityRequestShape,
        registration_timeout: timedelta,
        source: CapacityReservationSource = CapacityReservationSource.PlacementMiss,
        target_worker_id: str = "",
        desired_unit: int = 0,
        now: datetime | None = None,
    ) -> CapacityReservationDecision:
        current_time = now or utc_now()
        existing = self.allocation_for_request(request.container_id)
        if existing is not None:
            reservation = self.get(existing.reservation_id)
            if reservation is not None and reservation.open:
                return CapacityReservationDecision(
                    reservation=reservation,
                    allocation=existing,
                )
            self.release_allocation(request.container_id)
            if reservation is not None:
                self.release_terminal(reservation.id, now=current_time)

        reservation = self._compatible_open_reservation(
            capacity_owner_id,
            request,
            target_worker_id=target_worker_id,
        )
        created = reservation is None
        if reservation is None:
            if target_worker_id and any(
                candidate.open and candidate.target_worker_id == target_worker_id
                for candidate in self.list_for_owner(capacity_owner_id)
            ):
                raise CapacityReservationVersionConflictError(
                    f"pending worker {target_worker_id} capacity is already reserved"
                )
            reservation_id = str(uuid4())
            reservation = CapacityProvisioningReservation(
                id=reservation_id,
                capacity_owner_id=capacity_owner_id,
                pool_name=pool_name,
                owner_kind=owner_kind,
                source=source,
                acquisition_shape=shape,
                operation_id=reservation_id,
                target_worker_id=target_worker_id,
                desired_unit=desired_unit,
                registration_deadline_at=current_time + registration_timeout,
                created_at=current_time,
                updated_at=current_time,
            )
        allocation = CapacityReservationAllocation(
            reservation_id=reservation.id,
            container_id=request.container_id,
            workspace_id=request.workspace_id,
            cpu_millicores=request.cpu_millicores,
            memory_mib=capacity_memory_mib(request.memory_mib),
            gpu_count=gpu_count_for_capacity(
                request.gpu_type,
                request.gpu_request,
                request.gpu_count,
            ),
            created_at=current_time,
        )
        self._store_reservation_and_allocation(reservation, allocation, created=created)
        return CapacityReservationDecision(
            reservation=reservation,
            allocation=allocation,
            created=created,
        )

    def get(self, reservation_id: str) -> CapacityProvisioningReservation | None:
        raw = self.redis.get(self.keys.reservation(reservation_id))
        if raw is None:
            return None
        return CapacityProvisioningReservation.model_validate_json(redis_text(raw))

    def list_all(self) -> list[CapacityProvisioningReservation]:
        reservations = [
            reservation
            for reservation_id in _redis_strings(
                self.redis.set_members(self.keys.reservation_index())
            )
            if (reservation := self.get(reservation_id)) is not None
        ]
        return sorted(reservations, key=lambda item: (item.created_at, item.id))

    def list_for_owner(self, capacity_owner_id: str) -> list[CapacityProvisioningReservation]:
        reservations = [
            reservation
            for reservation_id in _redis_strings(
                self.redis.set_members(self.keys.owner_reservations(capacity_owner_id))
            )
            if (reservation := self.get(reservation_id)) is not None
        ]
        return sorted(reservations, key=lambda item: (item.created_at, item.id))

    def allocations_for(
        self,
        reservation_id: str,
    ) -> list[CapacityReservationAllocation]:
        allocations: list[CapacityReservationAllocation] = []
        for container_id in _redis_strings(
            self.redis.set_members(self.keys.allocations(reservation_id))
        ):
            raw = self.redis.get(self.keys.allocation(reservation_id, container_id))
            if raw is not None:
                allocations.append(
                    CapacityReservationAllocation.model_validate_json(redis_text(raw))
                )
        return sorted(allocations, key=lambda item: (item.created_at, item.container_id))

    def allocation_for_request(
        self,
        container_id: str,
    ) -> CapacityReservationAllocation | None:
        raw_reservation_id = self.redis.get(self.keys.request_reservation(container_id))
        if raw_reservation_id is None:
            return None
        reservation_id = redis_text(raw_reservation_id)
        raw = self.redis.get(self.keys.allocation(reservation_id, container_id))
        if raw is None:
            self.redis.delete(self.keys.request_reservation(container_id))
            return None
        return CapacityReservationAllocation.model_validate_json(redis_text(raw))

    def dispatch_allocation(
        self,
        container_id: str,
    ) -> CapacityReservationDispatchAllocation | None:
        allocation = self.allocation_for_request(container_id)
        if allocation is None:
            return None
        return CapacityReservationDispatchAllocation(
            reservation_id=allocation.reservation_id,
            request_index_key=self.keys.request_reservation(container_id),
            allocation_key=self.keys.allocation(allocation.reservation_id, container_id),
            allocation_index_key=self.keys.allocations(allocation.reservation_id),
        )

    def update(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        expected_resource_version: int,
        now: datetime | None = None,
    ) -> CapacityProvisioningReservation:
        current = self.get(reservation.id)
        if current is None or current.resource_version != expected_resource_version:
            raise CapacityReservationVersionConflictError(reservation.id)
        if (
            reservation.status is not current.status
            and reservation.status not in _allowed_next_statuses(current.status)
        ):
            raise CapacityReservationStateTransitionError(
                f"capacity reservation cannot transition from {current.status.value} "
                f"to {reservation.status.value}"
            )
        stored = reservation.model_copy(
            update={
                "resource_version": expected_resource_version + 1,
                "updated_at": now or utc_now(),
            }
        )
        self.redis.set(self.keys.reservation(stored.id), stored.model_dump_json())
        return stored

    def prepare_release(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        release_target_unit: int,
        now: datetime,
    ) -> CapacityProvisioningReservation:
        current = self.get(reservation.id)
        if current is None or current.resource_version != reservation.resource_version:
            raise CapacityReservationVersionConflictError(reservation.id)
        if current.release_requested:
            return current
        prepared = current.model_copy(
            update={
                "resource_version": current.resource_version + 1,
                "release_requested": True,
                "release_target_unit": release_target_unit,
                "updated_at": now,
            }
        )
        self.redis.set(self.keys.reservation(prepared.id), prepared.model_dump_json())
        return prepared

    def release_allocation(self, container_id: str) -> bool:
        allocation = self.allocation_for_request(container_id)
        if allocation is None:
            return False
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.delete(self.keys.request_reservation(container_id))
        pipeline.delete(self.keys.allocation(allocation.reservation_id, container_id))
        pipeline.set_remove(self.keys.allocations(allocation.reservation_id), container_id)
        pipeline.execute()
        return True

    def release_terminal(
        self,
        reservation_id: str,
        *,
        now: datetime | None = None,
    ) -> CapacityProvisioningReservation | None:
        reservation = self.get(reservation_id)
        if reservation is None or self.allocations_for(reservation_id):
            return reservation
        if reservation.status is CapacityReservationStatus.Released:
            return reservation
        if reservation.status in {
            CapacityReservationStatus.Reserved,
            CapacityReservationStatus.Provisioning,
        }:
            reservation = self.update(
                reservation.model_copy(
                    update={
                        "status": CapacityReservationStatus.Failed,
                        "terminal_reason": (
                            reservation.terminal_reason
                            or "reservation released before registration"
                        ),
                    }
                ),
                expected_resource_version=reservation.resource_version,
                now=now,
            )
        return self.update(
            reservation.model_copy(update={"status": CapacityReservationStatus.Released}),
            expected_resource_version=reservation.resource_version,
            now=now,
        )

    def delete_released(
        self,
        *,
        before: datetime,
    ) -> list[str]:
        removed: list[str] = []
        for reservation in self.list_all():
            if reservation.status is not CapacityReservationStatus.Released:
                continue
            if reservation.updated_at > before:
                continue
            pipeline = self.redis.pipeline(transaction=True)
            pipeline.delete(self.keys.reservation(reservation.id))
            pipeline.delete(self.keys.allocations(reservation.id))
            pipeline.set_remove(self.keys.reservation_index(), reservation.id)
            pipeline.set_remove(
                self.keys.owner_reservations(reservation.capacity_owner_id),
                reservation.id,
            )
            pipeline.execute()
            removed.append(reservation.id)
        return removed

    def _compatible_open_reservation(
        self,
        capacity_owner_id: str,
        request: SchedulerWorkerRequest,
        *,
        target_worker_id: str,
    ) -> CapacityProvisioningReservation | None:
        for reservation in self.list_for_owner(capacity_owner_id):
            if (
                not reservation.accepting_allocations
                or reservation.target_worker_id != target_worker_id
                or not reservation.allocation_shape.can_host(request)
            ):
                continue
            allocations = self.allocations_for(reservation.id)
            used_cpu = sum(item.cpu_millicores for item in allocations)
            used_memory = sum(item.memory_mib for item in allocations)
            used_gpu = sum(item.gpu_count for item in allocations)
            requested_gpu = gpu_count_for_capacity(
                request.gpu_type,
                request.gpu_request,
                request.gpu_count,
            )
            if (
                used_cpu + request.cpu_millicores <= reservation.allocation_shape.cpu_millicores
                and used_memory + capacity_memory_mib(request.memory_mib)
                <= reservation.allocation_shape.memory_mib
                and used_gpu + requested_gpu <= reservation.allocation_shape.gpu_count
            ):
                return reservation
        return None

    def _store_reservation_and_allocation(
        self,
        reservation: CapacityProvisioningReservation,
        allocation: CapacityReservationAllocation,
        *,
        created: bool,
    ) -> None:
        pipeline = self.redis.pipeline(transaction=True)
        if created:
            pipeline.set(self.keys.reservation(reservation.id), reservation.model_dump_json())
            pipeline.set_add(self.keys.reservation_index(), reservation.id)
            pipeline.set_add(
                self.keys.owner_reservations(reservation.capacity_owner_id),
                reservation.id,
            )
        pipeline.set(
            self.keys.allocation(reservation.id, allocation.container_id),
            allocation.model_dump_json(),
        )
        pipeline.set(self.keys.request_reservation(allocation.container_id), reservation.id)
        pipeline.set_add(self.keys.allocations(reservation.id), allocation.container_id)
        pipeline.execute()


@dataclass(slots=True)
class CapacityReservationService:
    reservations: RedisCapacityReservationRepository
    controllers: Callable[[], Iterable[CapacityAcquisitionController]]
    pending_owners: Callable[[], Iterable[PendingCapacityOwner]] = tuple
    allocation_owners: CapacityAllocationOwnerDirectory | None = None

    @contextmanager
    def mutation_lock(self, capacity_owner_id: str) -> Iterator[None]:
        with self.reservations.mutation_lock(capacity_owner_id):
            yield

    def can_acquire(self, request: SchedulerWorkerRequest) -> bool:
        return self._controller_for_request(request) is not None

    def resolve_request(self, request: SchedulerWorkerRequest) -> SchedulerWorkerRequest:
        controller = self._controller_for_request(request)
        if controller is None:
            return request
        if request.capacity_owner_id and request.capacity_owner_id != controller.capacity_owner_id:
            raise ValueError("request capacity owner does not match its selected pool")
        if (
            request.capacity_owner_id == controller.capacity_owner_id
            and request.pool_selector == controller.pool_name
        ):
            return request
        return request.model_copy(
            update={
                "pool_selector": controller.pool_name,
                "capacity_owner_id": controller.capacity_owner_id,
            }
        )

    def reserve_pending(
        self,
        request: SchedulerWorkerRequest,
        worker: SchedulerWorkerRecord,
        *,
        now: datetime | None = None,
    ) -> CapacityAcquisitionResult:
        pending_owner = self._pending_owner_for_worker(request, worker)
        if pending_owner is None:
            return _unsupported_result(request, "pending worker has no matching capacity owner")
        current_time = now or utc_now()
        with self.reservations.mutation_lock(pending_owner.capacity_owner_id):
            decision = self.reservations.reserve(
                capacity_owner_id=pending_owner.capacity_owner_id,
                pool_name=pending_owner.pool_name,
                owner_kind=pending_owner.owner_kind,
                request=request,
                shape=_shape_from_worker(worker),
                registration_timeout=timedelta(seconds=pending_owner.registration_timeout_seconds),
                source=CapacityReservationSource.PendingWorker,
                target_worker_id=worker.worker_id,
                now=current_time,
            )
            reservation = decision.reservation
            if decision.created:
                reservation = self.reservations.update(
                    reservation.model_copy(
                        update={"status": CapacityReservationStatus.Provisioning}
                    ),
                    expected_resource_version=reservation.resource_version,
                    now=current_time,
                )
            return CapacityAcquisitionResult(
                status=CapacityAcquisitionStatus.ExistingPending,
                capacity_owner_id=reservation.capacity_owner_id,
                reservation_id=reservation.id,
                operation_id=reservation.operation_id,
                desired_unit=reservation.desired_unit,
                target_worker_id=worker.worker_id,
                reason="reserved matching pending worker capacity",
            )

    def acquire(
        self,
        request: SchedulerWorkerRequest,
        *,
        now: datetime | None = None,
    ) -> CapacityAcquisitionResult:
        controllers = self._controllers_for_request(request)
        if not controllers:
            return _unsupported_result(request, "no capacity owner accepts the request")
        current_time = now or utc_now()
        strict = bool(request.capacity_owner_id) or _request_has_strict_pool(request)
        last_result: CapacityAcquisitionResult | None = None
        for controller in controllers:
            try:
                result = self._acquire_from_controller(
                    request,
                    controller,
                    now=current_time,
                )
            except Exception:
                if strict:
                    raise
                continue
            last_result = result
            if strict or result.status in {
                CapacityAcquisitionStatus.ExistingPending,
                CapacityAcquisitionStatus.Requested,
            }:
                return result
            self._release_failed_failover_allocation(result, request, now=current_time)
        return last_result or _unsupported_result(
            request,
            "all compatible capacity owners are unavailable",
        )

    def _acquire_from_controller(
        self,
        request: SchedulerWorkerRequest,
        controller: CapacityAcquisitionController,
        *,
        now: datetime,
    ) -> CapacityAcquisitionResult:
        with self.reservations.mutation_lock(controller.capacity_owner_id):
            releasing = next(
                (
                    reservation
                    for reservation in self.reservations.list_for_owner(
                        controller.capacity_owner_id
                    )
                    if reservation.release_requested
                ),
                None,
            )
            if releasing is not None:
                return CapacityAcquisitionResult(
                    status=CapacityAcquisitionStatus.TemporarilyUnavailable,
                    capacity_owner_id=controller.capacity_owner_id,
                    reservation_id=releasing.id,
                    operation_id=releasing.operation_id,
                    desired_unit=releasing.release_target_unit or releasing.desired_unit,
                    reason="capacity owner is finishing an earlier release intent",
                )
            decision = self.reservations.reserve(
                capacity_owner_id=controller.capacity_owner_id,
                pool_name=controller.pool_name,
                owner_kind=controller.owner_kind,
                request=request,
                shape=controller.reservation_shape(request),
                registration_timeout=controller.registration_timeout,
                now=now,
            )
            reservation = decision.reservation
            owner_reservations = tuple(
                self.reservations.list_for_owner(controller.capacity_owner_id)
            )
            if reservation.source is CapacityReservationSource.PendingWorker:
                # This reservation claims a worker that is already booting, so it
                # asks the provider for nothing and carries no desired unit. The
                # controller would refuse it on exactly that ground. `reconcile`
                # skips it for the same reason.
                return CapacityAcquisitionResult(
                    status=CapacityAcquisitionStatus.ExistingPending,
                    capacity_owner_id=reservation.capacity_owner_id,
                    reservation_id=reservation.id,
                    operation_id=reservation.operation_id,
                    desired_unit=reservation.desired_unit,
                    target_worker_id=reservation.target_worker_id,
                    reason="pending worker capacity reservation is awaiting registration",
                )
            if (
                not decision.created
                and reservation.status is not CapacityReservationStatus.Reserved
            ):
                result = controller.reconcile(
                    reservation,
                    owner_reservations=owner_reservations,
                    now=now,
                )
                self._record_acquisition_result(reservation, result, now=now)
                return result
            planned = controller.plan_acquisition(
                reservation,
                owner_reservations=owner_reservations,
                now=now,
            )
            reservation = self._record_acquisition_result(
                reservation,
                planned,
                now=now,
            )
            if planned.status is not CapacityAcquisitionStatus.Requested:
                return planned
            result = controller.ensure_acquisition(reservation, now=now)
            self._record_acquisition_result(reservation, result, now=now)
            return result

    def _release_failed_failover_allocation(
        self,
        result: CapacityAcquisitionResult,
        request: SchedulerWorkerRequest,
        *,
        now: datetime,
    ) -> None:
        allocation = self.reservations.allocation_for_request(request.container_id)
        if allocation is None or allocation.reservation_id != result.reservation_id:
            return
        self.reservations.release_allocation(request.container_id)
        self.reservations.release_terminal(result.reservation_id, now=now)

    def registered_worker_id(self, container_id: str) -> str:
        allocation = self.reservations.allocation_for_request(container_id)
        if allocation is None:
            return ""
        reservation = self.reservations.get(allocation.reservation_id)
        if reservation is None or reservation.status is not CapacityReservationStatus.Registered:
            return ""
        return reservation.target_worker_id

    def dispatch_allocation(
        self,
        container_id: str,
    ) -> CapacityReservationDispatchAllocation | None:
        return self.reservations.dispatch_allocation(container_id)

    def prepare_dispatch(
        self,
        container_id: str,
        worker: SchedulerWorkerRecord,
        *,
        now: datetime | None = None,
    ) -> CapacityReservationDispatchAllocation | None:
        """Transfer a provisioning unit to its registered worker before dispatch."""

        allocation = self.reservations.allocation_for_request(container_id)
        if allocation is None:
            return None
        reservation = self.reservations.get(allocation.reservation_id)
        if reservation is None or not reservation.open:
            raise CapacityReservationVersionConflictError(
                f"capacity reservation is unavailable during dispatch: {allocation.reservation_id}"
            )
        allocations = self.reservations.allocations_for(reservation.id)
        if not reservation_matches_worker(reservation, worker, allocations=allocations):
            raise CapacityReservationConflictError(
                f"worker {worker.worker_id} does not own capacity reservation {reservation.id}"
            )
        if (
            reservation.status is not CapacityReservationStatus.Registered
            or reservation.target_worker_id != worker.worker_id
            or reservation.target_machine_id != worker.machine_id
            or reservation.schedulable_shape != _schedulable_shape(reservation, worker)
        ):
            reservation = self.reservations.update(
                reservation.model_copy(
                    update={
                        "status": CapacityReservationStatus.Registered,
                        "schedulable_shape": _schedulable_shape(reservation, worker),
                        "target_worker_id": worker.worker_id,
                        "target_machine_id": worker.machine_id,
                        "terminal_reason": "",
                    }
                ),
                expected_resource_version=reservation.resource_version,
                now=now,
            )
        return self.reservations.dispatch_allocation(container_id)

    def reserved_worker_capacity(self) -> dict[str, WorkerReservedCapacity]:
        reserved: dict[str, WorkerReservedCapacity] = {}
        for reservation in self.reservations.list_all():
            if not reservation.open or not reservation.target_worker_id:
                continue
            total = reserved.setdefault(
                reservation.target_worker_id,
                WorkerReservedCapacity(),
            )
            for allocation in self.reservations.allocations_for(reservation.id):
                total.cpu_millicores += allocation.cpu_millicores
                total.memory_mib += allocation.memory_mib
                total.gpu_count += allocation.gpu_count
        return reserved

    def release_request(
        self,
        container_id: str,
        *,
        workers: Iterable[SchedulerWorkerRecord] = (),
        now: datetime | None = None,
    ) -> None:
        allocation = self.reservations.allocation_for_request(container_id)
        if allocation is None:
            return
        reservation = self.reservations.get(allocation.reservation_id)
        if reservation is None:
            self.reservations.release_allocation(container_id)
            return
        with self.reservations.mutation_lock(reservation.capacity_owner_id):
            self.reservations.release_allocation(container_id)
            if self.reservations.allocations_for(reservation.id):
                return
            self._release_unallocated_reservation(
                reservation,
                workers=workers,
                worker_reservations={},
                now=now or utc_now(),
            )

    def reconcile(
        self,
        workers: Iterable[SchedulerWorkerRecord],
        *,
        now: datetime | None = None,
    ) -> list[CapacityProvisioningReservation]:
        current_time = now or utc_now()
        worker_records = list(workers)
        controllers = {
            controller.capacity_owner_id: controller for controller in self.controllers()
        }
        self._reconcile_pool_sizing(
            tuple(controllers.values()),
            now=current_time,
        )
        worker_reservations = {
            reservation.target_worker_id: reservation.id
            for reservation in self.reservations.list_all()
            if reservation.status is CapacityReservationStatus.Registered
            and reservation.target_worker_id
        }
        reconciled: list[CapacityProvisioningReservation] = []
        for snapshot in self.reservations.list_all():
            controller = controllers.get(snapshot.capacity_owner_id)
            if snapshot.status is CapacityReservationStatus.Released:
                continue
            try:
                with self.reservations.mutation_lock(snapshot.capacity_owner_id):
                    reservation = self.reservations.get(snapshot.id)
                    if reservation is None:
                        continue
                    allocations = self._prune_inactive_allocations(
                        self.reservations.allocations_for(reservation.id)
                    )
                    if (
                        reservation.status
                        in {
                            CapacityReservationStatus.Failed,
                            CapacityReservationStatus.Expired,
                        }
                        and allocations
                    ):
                        reconciled.append(reservation)
                        continue
                    if not allocations:
                        reconciled.append(
                            self._release_unallocated_reservation(
                                reservation,
                                workers=worker_records,
                                worker_reservations=worker_reservations,
                                now=current_time,
                            )
                        )
                        continue
                    worker = _registered_worker_for_reservation(
                        reservation,
                        worker_records,
                        worker_reservations=worker_reservations,
                        allocations=allocations,
                    )
                    if worker is not None:
                        worker_reservations[worker.worker_id] = reservation.id
                        if (
                            reservation.status is not CapacityReservationStatus.Registered
                            or reservation.target_worker_id != worker.worker_id
                            or reservation.target_machine_id != worker.machine_id
                            or reservation.schedulable_shape
                            != _schedulable_shape(reservation, worker)
                        ):
                            reservation = self.reservations.update(
                                reservation.model_copy(
                                    update={
                                        "status": CapacityReservationStatus.Registered,
                                        "schedulable_shape": _schedulable_shape(
                                            reservation, worker
                                        ),
                                        "target_worker_id": worker.worker_id,
                                        "target_machine_id": worker.machine_id,
                                        "terminal_reason": "",
                                    }
                                ),
                                expected_resource_version=reservation.resource_version,
                                now=current_time,
                            )
                        reconciled.append(reservation)
                        continue
                    if current_time >= reservation.registration_deadline_at:
                        if reservation.acquisition_created:
                            if controller is None:
                                reconciled.append(reservation)
                                continue
                            reservation, release_result = self._release_owned_capacity(
                                reservation,
                                controller,
                                now=current_time,
                            )
                            release_confirmed = release_result.status in {
                                CapacityAcquisitionStatus.ExistingPending,
                                CapacityAcquisitionStatus.Requested,
                            }
                        else:
                            release_result = CapacityAcquisitionResult(
                                status=CapacityAcquisitionStatus.ExistingPending,
                                capacity_owner_id=reservation.capacity_owner_id,
                                reservation_id=reservation.id,
                                operation_id=reservation.operation_id,
                                desired_unit=reservation.desired_unit,
                                reason="unowned pending capacity registration expired",
                            )
                            release_confirmed = True
                        if release_confirmed:
                            reservation = self.reservations.update(
                                reservation.model_copy(
                                    update={
                                        "status": CapacityReservationStatus.Expired,
                                        "acquisition_created": False,
                                        "release_requested": False,
                                        "terminal_reason": release_result.reason
                                        or "worker registration deadline expired",
                                    }
                                ),
                                expected_resource_version=reservation.resource_version,
                                now=current_time,
                            )
                        else:
                            reservation = self.reservations.update(
                                reservation.model_copy(
                                    update={"terminal_reason": release_result.reason}
                                ),
                                expected_resource_version=reservation.resource_version,
                                now=current_time,
                            )
                        reconciled.append(reservation)
                        continue
                    if reservation.source is CapacityReservationSource.PendingWorker:
                        reconciled.append(reservation)
                        continue
                    if controller is None:
                        reconciled.append(reservation)
                        continue
                    result = controller.reconcile(
                        reservation,
                        owner_reservations=tuple(
                            self.reservations.list_for_owner(reservation.capacity_owner_id)
                        ),
                        now=current_time,
                    )
                    reservation = self._record_acquisition_result(
                        reservation,
                        result,
                        now=current_time,
                    )
                    reconciled.append(reservation)
            except CapacityReservationConflictError:
                continue
        self.reservations.delete_released(
            before=current_time - timedelta(seconds=DEFAULT_CAPACITY_RESERVATION_RETENTION_SECONDS)
        )
        return reconciled

    def _prune_inactive_allocations(
        self,
        allocations: Iterable[CapacityReservationAllocation],
    ) -> list[CapacityReservationAllocation]:
        allocation_owners = self.allocation_owners
        if allocation_owners is None:
            return list(allocations)
        active: list[CapacityReservationAllocation] = []
        for allocation in allocations:
            if allocation_owners.is_active(
                workspace_id=allocation.workspace_id,
                container_id=allocation.container_id,
            ):
                active.append(allocation)
                continue
            self.reservations.release_allocation(allocation.container_id)
        return active

    def _reconcile_pool_sizing(
        self,
        controllers: Sequence[CapacityAcquisitionController],
        *,
        now: datetime,
    ) -> None:
        for controller in sorted(controllers, key=lambda item: item.capacity_owner_id):
            try:
                with self.reservations.mutation_lock(controller.capacity_owner_id):
                    owner_reservations = tuple(
                        reservation
                        for reservation in self.reservations.list_for_owner(
                            controller.capacity_owner_id
                        )
                        if reservation.open
                    )
                    allocations = tuple(
                        allocation
                        for reservation in owner_reservations
                        for allocation in self.reservations.allocations_for(reservation.id)
                    )
                    controller.reconcile_sizing(
                        reservations=owner_reservations,
                        allocations=allocations,
                        now=now,
                    )
            except CapacityReservationConflictError:
                continue

    def has_open_reservations(self, capacity_owner_id: str) -> bool:
        return any(
            reservation.open for reservation in self.reservations.list_for_owner(capacity_owner_id)
        )

    def _release_unallocated_reservation(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        workers: Iterable[SchedulerWorkerRecord],
        worker_reservations: dict[str, str],
        now: datetime,
    ) -> CapacityProvisioningReservation:
        if reservation.status is CapacityReservationStatus.Registered:
            return self.reservations.release_terminal(reservation.id, now=now) or reservation
        worker = _registered_worker_for_reservation(
            reservation,
            workers,
            worker_reservations=worker_reservations,
            allocations=(),
        )
        if worker is not None:
            registered = self.reservations.update(
                reservation.model_copy(
                    update={
                        "status": CapacityReservationStatus.Registered,
                        "schedulable_shape": _schedulable_shape(reservation, worker),
                        "target_worker_id": worker.worker_id,
                        "target_machine_id": worker.machine_id,
                        "terminal_reason": "",
                    }
                ),
                expected_resource_version=reservation.resource_version,
                now=now,
            )
            return self.reservations.release_terminal(registered.id, now=now) or registered
        if reservation.acquisition_created:
            controller = next(
                (
                    candidate
                    for candidate in self.controllers()
                    if candidate.capacity_owner_id == reservation.capacity_owner_id
                ),
                None,
            )
            if controller is None:
                return reservation
            reservation, release_result = self._release_owned_capacity(
                reservation,
                controller,
                now=now,
            )
            if release_result.status in {
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                CapacityAcquisitionStatus.Unsupported,
            }:
                return self.reservations.update(
                    reservation.model_copy(update={"terminal_reason": release_result.reason}),
                    expected_resource_version=reservation.resource_version,
                    now=now,
                )
            reservation = self.reservations.update(
                reservation.model_copy(
                    update={
                        "acquisition_created": False,
                        "release_requested": False,
                    }
                ),
                expected_resource_version=reservation.resource_version,
                now=now,
            )
        return self.reservations.release_terminal(reservation.id, now=now) or reservation

    def _release_owned_capacity(
        self,
        reservation: CapacityProvisioningReservation,
        controller: CapacityAcquisitionController,
        *,
        now: datetime,
    ) -> tuple[CapacityProvisioningReservation, CapacityAcquisitionResult]:
        owner_reservations = tuple(self.reservations.list_for_owner(reservation.capacity_owner_id))
        if not reservation.release_requested:
            release_plan = controller.plan_release(
                reservation,
                owner_reservations=owner_reservations,
                now=now,
            )
            if release_plan.status in {
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                CapacityAcquisitionStatus.Unsupported,
            }:
                return reservation, release_plan
            reservation = self.reservations.prepare_release(
                reservation,
                release_target_unit=release_plan.desired_unit,
                now=now,
            )
            owner_reservations = tuple(
                self.reservations.list_for_owner(reservation.capacity_owner_id)
            )
        return reservation, controller.release(
            reservation,
            owner_reservations=owner_reservations,
            now=now,
        )

    def _record_acquisition_result(
        self,
        reservation: CapacityProvisioningReservation,
        result: CapacityAcquisitionResult,
        *,
        now: datetime,
    ) -> CapacityProvisioningReservation:
        if result.reservation_id != reservation.id:
            raise ValueError("capacity acquisition returned a different reservation identity")
        if result.capacity_owner_id != reservation.capacity_owner_id:
            raise ValueError("capacity acquisition returned a different owner identity")
        next_status = {
            CapacityAcquisitionStatus.ExistingPending: CapacityReservationStatus.Provisioning,
            CapacityAcquisitionStatus.Requested: CapacityReservationStatus.Provisioning,
            CapacityAcquisitionStatus.AtLimit: CapacityReservationStatus.Failed,
            CapacityAcquisitionStatus.TemporarilyUnavailable: reservation.status,
            CapacityAcquisitionStatus.Unsupported: CapacityReservationStatus.Failed,
        }[result.status]
        status = (
            CapacityReservationStatus.Registered
            if reservation.status is CapacityReservationStatus.Registered
            else next_status
        )
        return self.reservations.update(
            reservation.model_copy(
                update={
                    "status": status,
                    "desired_unit": result.desired_unit,
                    "acquisition_created": (
                        reservation.acquisition_created
                        or result.status is CapacityAcquisitionStatus.Requested
                    ),
                    "target_worker_id": result.target_worker_id or reservation.target_worker_id,
                    "target_machine_id": result.target_machine_id or reservation.target_machine_id,
                    "terminal_reason": (
                        result.reason if status is CapacityReservationStatus.Failed else ""
                    ),
                }
            ),
            expected_resource_version=reservation.resource_version,
            now=now,
        )

    def _controller_for_request(
        self,
        request: SchedulerWorkerRequest,
    ) -> CapacityAcquisitionController | None:
        candidates = self._controllers_for_request(request)
        return candidates[0] if candidates else None

    def _controllers_for_request(
        self,
        request: SchedulerWorkerRequest,
    ) -> list[CapacityAcquisitionController]:
        candidates = [
            controller for controller in self.controllers() if controller.accepts(request)
        ]
        if not candidates:
            return []
        if request.capacity_owner_id or _request_has_strict_pool(request):
            candidates.sort(key=lambda controller: controller.capacity_owner_id)
            return candidates
        current_time = utc_now()
        candidates.sort(
            key=lambda controller: capacity_pool_selection_key(
                health=controller.operational_health(now=current_time),
                priority=controller.priority,
                capacity_owner_id=controller.capacity_owner_id,
            )
        )
        return candidates

    def _controller_for_owner(
        self,
        capacity_owner_id: str,
    ) -> CapacityAcquisitionController | None:
        return next(
            (
                controller
                for controller in self.controllers()
                if controller.capacity_owner_id == capacity_owner_id
            ),
            None,
        )

    def _pending_owner_for_worker(
        self,
        request: SchedulerWorkerRequest,
        worker: SchedulerWorkerRecord,
    ) -> PendingCapacityOwner | None:
        candidates = [owner for owner in self.pending_owners() if owner.accepts(request, worker)]
        if not candidates:
            controller = self._controller_for_owner(worker.capacity_owner_id)
            if controller is not None and (
                request.capacity_owner_id in {"", controller.capacity_owner_id}
                and request.pool_selector in {"", controller.pool_name}
                and worker.pool_name == controller.pool_name
            ):
                candidates.append(
                    PendingCapacityOwner(
                        capacity_owner_id=controller.capacity_owner_id,
                        owner_kind=controller.owner_kind,
                        pool_name=controller.pool_name,
                        registration_timeout_seconds=int(
                            controller.registration_timeout.total_seconds()
                        ),
                    )
                )
        if not candidates:
            return None
        candidates.sort(key=lambda owner: owner.capacity_owner_id)
        return candidates[0]


def reservation_shape_for_request(
    request: SchedulerWorkerRequest,
    *,
    worker_cpu_millicores: int,
    worker_memory_mib: int,
    worker_gpu_type: str,
    worker_gpu_count: int,
    worker_runtimes: Iterable[str],
    worker_preemptible: bool,
) -> CapacityRequestShape:
    requested_gpu_count = gpu_count_for_capacity(
        request.gpu_type,
        request.gpu_request,
        request.gpu_count,
    )
    requested_gpu_type = _requested_gpu_type(request)
    gpu_count = max(worker_gpu_count, requested_gpu_count)
    gpu_type = worker_gpu_type or requested_gpu_type if gpu_count > 0 else ""
    return CapacityRequestShape(
        cpu_millicores=max(worker_cpu_millicores, request.cpu_millicores),
        memory_mib=max(worker_memory_mib, capacity_memory_mib(request.memory_mib)),
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        runtime_class=request.runtime_class,
        runtime_classes=tuple(dict.fromkeys(runtime for runtime in worker_runtimes if runtime)),
        docker_enabled=request.docker_enabled,
        preemptible=worker_preemptible,
    )


def _request_has_strict_pool(request: SchedulerWorkerRequest) -> bool:
    return (
        bool(request.pool_selector)
        and request.placement_source is ComputePlacementSource.AttachedPool
    )


def _sizing_state(
    pool: Pool,
    states: CapacityPoolSizingStateService,
    registered_units: int,
) -> CapacityPoolSizingState:
    state = states.get_pool_sizing_state(pool.capacity_owner_id)
    if state.pool_name != pool.name:
        raise ValueError("worker-pool sizing state belongs to a different pool")
    updates: dict[str, str | int | bool | datetime | None] = {}
    if not state.initial_target_reached and registered_units >= pool.initial_workers:
        updates["initial_target_reached"] = True
    if state.operation_id and registered_units >= state.target_units:
        updates.update(
            {
                "operation_id": "",
                "target_units": 0,
                "operation_started_at": None,
                "retry_after_at": None,
                "consecutive_failures": 0,
            }
        )
    if not updates:
        return state
    return _save_sizing_state(states, state.model_copy(update=updates))


def _record_sizing_observation(
    states: CapacityPoolSizingStateService,
    state: CapacityPoolSizingState,
    plan: WorkerPoolSizingPlan,
) -> CapacityPoolSizingState:
    if state.initial_target_reached == plan.initial_target_reached:
        return state
    return _save_sizing_state(
        states, state.model_copy(update={"initial_target_reached": plan.initial_target_reached})
    )


def _save_sizing_state(
    states: CapacityPoolSizingStateService,
    state: CapacityPoolSizingState,
) -> CapacityPoolSizingState:
    return states.compare_and_set_pool_sizing_state(sizing_state_update(state))


def reservation_matches_worker(
    reservation: CapacityProvisioningReservation,
    worker: SchedulerWorkerRecord,
    *,
    allocations: Iterable[CapacityReservationAllocation] = (),
) -> bool:
    if worker.capacity_owner_id != reservation.capacity_owner_id:
        return False
    if worker.status not in {SchedulerWorkerStatus.Pending, SchedulerWorkerStatus.Available}:
        return False
    if reservation.target_worker_id and worker.worker_id != reservation.target_worker_id:
        return False
    if reservation.target_machine_id and worker.machine_id != reservation.target_machine_id:
        return False
    if not reservation.acquisition_shape.worker_capabilities_match(worker):
        return False
    allocated_cpu = 0
    allocated_memory = 0
    allocated_gpu = 0
    for allocation in allocations:
        allocated_cpu += allocation.cpu_millicores
        allocated_memory += allocation.memory_mib
        allocated_gpu += allocation.gpu_count
    return (
        worker.total_cpu_millicores >= allocated_cpu
        and worker.total_memory_mib >= allocated_memory
        and worker.total_gpu_count >= allocated_gpu
    )


def _schedulable_shape(
    reservation: CapacityProvisioningReservation,
    worker: SchedulerWorkerRecord,
) -> CapacityRequestShape:
    return reservation.acquisition_shape.model_copy(
        update={
            "cpu_millicores": worker.total_cpu_millicores,
            "memory_mib": worker.total_memory_mib,
            "gpu_type": worker.gpu_type if worker.total_gpu_count > 0 else "",
            "gpu_count": worker.total_gpu_count,
        }
    )


def _shape_from_worker(worker: SchedulerWorkerRecord) -> CapacityRequestShape:
    return CapacityRequestShape(
        cpu_millicores=worker.total_cpu_millicores,
        memory_mib=worker.total_memory_mib,
        gpu_type=worker.gpu_type if worker.total_gpu_count > 0 else "",
        gpu_count=worker.total_gpu_count,
        runtime_class=worker.runtime_class,
        runtime_classes=tuple(worker.runtime_classes),
        preemptible=worker.preemptible,
    )


def _registered_worker_for_reservation(
    reservation: CapacityProvisioningReservation,
    workers: Iterable[SchedulerWorkerRecord],
    *,
    worker_reservations: dict[str, str],
    allocations: Iterable[CapacityReservationAllocation] = (),
) -> SchedulerWorkerRecord | None:
    reservation_allocations = tuple(allocations)
    candidates = [
        worker
        for worker in workers
        if worker.status is SchedulerWorkerStatus.Available
        and worker_reservations.get(worker.worker_id, reservation.id) == reservation.id
        and reservation_matches_worker(
            reservation,
            worker,
            allocations=reservation_allocations,
        )
        and (
            worker.worker_id == reservation.target_worker_id
            or worker.created_at >= reservation.created_at
        )
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda worker: (worker.created_at, worker.worker_id))
    return candidates[0]


def _compute_capacity_shape(shape: CapacityRequestShape) -> ComputeCapacityShape:
    return ComputeCapacityShape(
        cpu_millicores=shape.cpu_millicores,
        memory_mib=shape.memory_mib,
        gpu_type=shape.gpu_type,
        gpu_count=shape.gpu_count,
        runtime=shape.runtime_class or shape.runtime_classes[0],
        preemptible=shape.preemptible,
    )


def _compute_acquisition_result(
    reservation: CapacityProvisioningReservation,
    result: ComputeCapacityResult,
) -> CapacityAcquisitionResult:
    if result.capacity_owner_id != reservation.capacity_owner_id:
        raise ValueError("compute acquisition returned a different capacity owner")
    if result.reservation_id != reservation.id:
        raise ValueError("compute acquisition returned a different reservation")
    return CapacityAcquisitionResult(
        status=CapacityAcquisitionStatus(result.status.value),
        capacity_owner_id=result.capacity_owner_id,
        reservation_id=result.reservation_id,
        operation_id=reservation.operation_id,
        desired_unit=result.desired_unit,
        target_machine_id=result.target_machine_id or "",
        reason=result.reason,
    )


def _unsupported_result(
    request: SchedulerWorkerRequest,
    reason: str,
) -> CapacityAcquisitionResult:
    return CapacityAcquisitionResult(
        status=CapacityAcquisitionStatus.Unsupported,
        capacity_owner_id="",
        reservation_id=request.container_id,
        operation_id=request.container_id,
        reason=reason,
    )


def _owner_key(capacity_owner_id: str) -> str:
    return capacity_owner_key_segment(capacity_owner_id)


def _allowed_next_statuses(
    status: CapacityReservationStatus,
) -> frozenset[CapacityReservationStatus]:
    if status is CapacityReservationStatus.Reserved:
        return frozenset(
            {
                CapacityReservationStatus.Provisioning,
                CapacityReservationStatus.Failed,
                CapacityReservationStatus.Expired,
            }
        )
    if status is CapacityReservationStatus.Provisioning:
        return frozenset(
            {
                CapacityReservationStatus.Registered,
                CapacityReservationStatus.Failed,
                CapacityReservationStatus.Expired,
            }
        )
    if status in {
        CapacityReservationStatus.Registered,
        CapacityReservationStatus.Failed,
        CapacityReservationStatus.Expired,
    }:
        return frozenset({CapacityReservationStatus.Released})
    return frozenset()


def _redis_strings(values: Iterable[str | bytes | int | float | bool]) -> list[str]:
    return sorted(redis_text(value) for value in values)


def _requested_gpu_type(request: SchedulerWorkerRequest) -> str:
    if request.gpu_type and request.gpu_type.lower() not in {"any", "gpu_any", "none"}:
        return request.gpu_type
    for candidate in request.gpu_request:
        if candidate and candidate.lower() not in {"any", "gpu_any", "none"}:
            return candidate
    return ""


def _gpu_matches(gpu_type: str, request: SchedulerWorkerRequest) -> bool:
    requested = {
        candidate.lower()
        for candidate in [request.gpu_type, *request.gpu_request]
        if candidate and candidate.lower() not in {"none"}
    }
    return (
        not requested
        or "any" in requested
        or "gpu_any" in requested
        or gpu_type.lower() in requested
    )


def _supports_docker(runtimes: Iterable[str]) -> bool:
    return bool(set(runtimes) & {"runc", "runsc", "gvisor", "sandboxed-oci"})


__all__ = [
    "CapacityAcquisitionController",
    "CapacityAcquisitionResult",
    "CapacityAcquisitionStatus",
    "CapacityProvisioningReservation",
    "CapacityRequestShape",
    "CapacityReservationAllocation",
    "CapacityReservationConflictError",
    "CapacityReservationDecision",
    "CapacityReservationLeaseLostError",
    "CapacityReservationLockContendedError",
    "CapacityReservationService",
    "CapacityReservationSource",
    "CapacityReservationStateTransitionError",
    "CapacityReservationStatus",
    "CapacityReservationVersionConflictError",
    "CapacityWorkerRepository",
    "ComputePoolCapacityController",
    "PendingCapacityOwner",
    "RedisCapacityReservationRepository",
    "reservation_matches_worker",
    "reservation_shape_for_request",
]
