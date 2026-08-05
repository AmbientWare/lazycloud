from __future__ import annotations

import logging
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
from shared.capacity import CapacityAcquisitionRequest as ComputeCapacityRequest
from shared.capacity import CapacityAcquisitionResult as ComputeCapacityResult
from shared.capacity import CapacityAcquisitionShape as ComputeCapacityShape
from shared.capacity import CapacityAcquisitionStatus as ComputeCapacityStatus
from shared.capacity import CapacityOwnerKind, CapacityPoolSizingSnapshot
from shared.capacity import CapacityReleaseRequest as ComputeCapacityReleaseRequest
from shared.compute_policy import ComputePoolRecord
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
    WorkerPoolSizingAction,
    WorkerPoolSizingAllocation,
    WorkerPoolSizingPlan,
    WorkerPoolSizingReservation,
    capacity_pool_operational_health,
    capacity_pool_selection_key,
    effective_pool_headroom,
    plan_worker_pool_sizing,
    scale_up_retry_at,
)
from scheduler.state import (
    CapacityReservationDispatchAllocation,
    WorkerReservedCapacity,
    capacity_memory_mib,
    capacity_owner_key_segment,
)

LOGGER = logging.getLogger(__name__)

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
    # Declaration order is the lifecycle order `update` enforces: a reservation
    # may repeat or advance a status, never move back to an earlier one.
    Pending = "pending"
    Registered = "registered"
    Failed = "failed"
    Released = "released"


_RESERVATION_LIFECYCLE_RANK: dict[CapacityReservationStatus, int] = {
    status: rank for rank, status in enumerate(CapacityReservationStatus)
}


class CapacityTerminalReason(StrEnum):
    ReleasedAfterRegistration = "released_after_registration"
    ReleasedBeforeRegistration = "released_before_registration"
    RegistrationDeadlineExpired = "registration_deadline_expired"
    AcquisitionUnsupported = "acquisition_unsupported"
    ReleaseUnconfirmed = "release_unconfirmed"


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
    status: CapacityReservationStatus = CapacityReservationStatus.Pending
    acquisition_shape: CapacityRequestShape
    schedulable_shape: CapacityRequestShape | None = None
    operation_id: str
    target_worker_id: str = ""
    target_machine_id: str = ""
    desired_unit: int = Field(default=0, ge=0)
    acquisition_created: bool = False
    release_requested: bool = False
    registration_deadline_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    terminal_reason: CapacityTerminalReason | None = None

    @property
    def open(self) -> bool:
        return self.status in {
            CapacityReservationStatus.Pending,
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
    capacity_owner_id: str = ""
    """Unit that served the request, empty when none accepted it."""
    reservation_id: str
    operation_id: str
    desired_unit: int = Field(default=0, ge=0)
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

    def ensure_capacity(
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


class ComputeCapacityService(Protocol):
    def pool_sizing_snapshot(self, capacity_owner_id: str) -> CapacityPoolSizingSnapshot: ...

    def ensure_capacity(
        self,
        request: ComputeCapacityRequest,
        *,
        minimum_unit: int = 0,
    ) -> ComputeCapacityResult: ...

    def release_acquired_capacity(
        self,
        request: ComputeCapacityReleaseRequest,
    ) -> ComputeCapacityResult: ...


@dataclass(slots=True)
class ComputePoolCapacityController:
    workspace_id: str
    pool: ComputePoolRecord
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
        _ = now
        return capacity_pool_operational_health(
            self.capacity_owner_id,
            self.workers.list_workers(),
            state=self.compute.pool_sizing_snapshot(self.capacity_owner_id),
        )

    def accepts(self, request: SchedulerWorkerRequest) -> bool:
        """Whether this unit is a candidate for the request.

        A named group admits every unit feeding it, which is what gives the
        acquisition loop more than one candidate to fail over between. A request
        that names no group falls back to the units marked default-eligible.
        """
        if not self.pool.scaling_enabled:
            return False
        if self.owner_kind not in {
            CapacityOwnerKind.ManagedPool,
            CapacityOwnerKind.PooledProvider,
        }:
            return False
        if request.workspace_id != self.workspace_id:
            return False
        if request.pool_selector:
            if request.pool_selector != self.pool.machine_pool:
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
        state = self.compute.pool_sizing_snapshot(self.capacity_owner_id)
        authoritative_units = max(
            registered_units,
            state.desired_units,
            state.pending_desired_units,
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
        retry_at = scale_up_retry_at(self.pool, state)
        if retry_at is not None and retry_at > now:
            return plan
        if state.pending_operation_id:
            # Compute committed to this unit but the provider never acknowledged
            # it. Re-drive that operation id; a fresh one would buy a second
            # machine for the same unit.
            return self._ensure_sizing_operation(
                state.pending_operation_id,
                minimum_unit=state.pending_desired_units,
                plan=plan,
            )
        if plan.action is not WorkerPoolSizingAction.ScaleUp:
            return plan
        return self._ensure_sizing_operation(
            str(uuid4()),
            minimum_unit=plan.target_units,
            plan=plan,
        )

    def _ensure_sizing_operation(
        self,
        operation_id: str,
        *,
        minimum_unit: int,
        plan: WorkerPoolSizingPlan,
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
            result = self.compute.ensure_capacity(
                ComputeCapacityRequest(
                    capacity_owner_id=self.capacity_owner_id,
                    reservation_id=operation_id,
                    operation_id=operation_id,
                    shape=shape,
                ),
                minimum_unit=minimum_unit,
            )
        except Exception:
            LOGGER.exception("compute pool sizing failed for %s", self.pool.name)
            return plan.model_copy(
                update={
                    "action": WorkerPoolSizingAction.Wait,
                    "reason": "compute pool sizing operation failed",
                }
            )
        if result.status in {
            ComputeCapacityStatus.TemporarilyUnavailable,
            ComputeCapacityStatus.Unsupported,
        }:
            return plan.model_copy(
                update={
                    "action": WorkerPoolSizingAction.Wait,
                    "reason": result.reason or "compute pool sizing is temporarily unavailable",
                }
            )
        if result.status is ComputeCapacityStatus.AtLimit:
            return plan.model_copy(
                update={
                    "action": WorkerPoolSizingAction.None_,
                    "target_units": result.desired_unit,
                    "reason": result.reason or "compute pool capacity is at limit",
                }
            )
        return plan.model_copy(update={"target_units": result.desired_unit})

    def ensure_capacity(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        owner_reservations: tuple[CapacityProvisioningReservation, ...],
        now: datetime,
    ) -> CapacityAcquisitionResult:
        _ = owner_reservations, now
        result = self.compute.ensure_capacity(
            ComputeCapacityRequest(
                capacity_owner_id=reservation.capacity_owner_id,
                reservation_id=reservation.id,
                operation_id=reservation.operation_id,
                shape=_compute_capacity_shape(reservation.acquisition_shape),
            )
        )
        return _compute_acquisition_result(reservation, result)

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
        lease_loss: list[tuple[str, BaseException | None]] = []

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
                except Exception as exc:
                    LOGGER.exception(
                        "capacity mutation lease renewal failed for owner %s",
                        capacity_owner_id,
                    )
                    lease_loss.append(("renewal failed", exc))
                    lease_lost.set()
                    return
                if renewed != 1:
                    lease_loss.append(("the lock was taken by another holder", None))
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
            except Exception as exc:
                LOGGER.exception(
                    "reading the capacity mutation lease failed for owner %s",
                    capacity_owner_id,
                )
                lease_loss.append(("reading the lock failed", exc))
                lease_lost.set()
            else:
                if current_token is None or redis_text(current_token) != token:
                    lease_loss.append(("the lock was taken by another holder", None))
                    lease_lost.set()
            try:
                release_token_lock(self.redis, key, token)
            except Exception as exc:
                LOGGER.exception(
                    "releasing the capacity mutation lease failed for owner %s",
                    capacity_owner_id,
                )
                lease_loss.append(("releasing the lock failed", exc))
                lease_lost.set()
            if lease_lost.is_set() and not body_failed:
                why, cause = lease_loss[0] if lease_loss else ("the lock was lost", None)
                raise CapacityReservationLeaseLostError(
                    f"capacity owner {capacity_owner_id} mutation lease was lost: {why}"
                ) from cause

    def reserve(
        self,
        *,
        capacity_owner_id: str,
        pool_name: str,
        owner_kind: CapacityOwnerKind,
        request: SchedulerWorkerRequest,
        shape: CapacityRequestShape,
        registration_timeout: timedelta,
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

        reservation = self._compatible_open_reservation(capacity_owner_id, request)
        created = reservation is None
        if reservation is None:
            reservation_id = str(uuid4())
            reservation = CapacityProvisioningReservation(
                id=reservation_id,
                capacity_owner_id=capacity_owner_id,
                pool_name=pool_name,
                owner_kind=owner_kind,
                acquisition_shape=shape,
                operation_id=reservation_id,
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
            _RESERVATION_LIFECYCLE_RANK[reservation.status]
            < _RESERVATION_LIFECYCLE_RANK[current.status]
        ):
            raise CapacityReservationStateTransitionError(
                f"capacity reservation cannot move back from {current.status.value} "
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
        return self.update(
            reservation.model_copy(
                update={
                    "status": CapacityReservationStatus.Released,
                    "terminal_reason": reservation.terminal_reason
                    or _release_reason(reservation.status),
                }
            ),
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
    ) -> CapacityProvisioningReservation | None:
        for reservation in self.list_for_owner(capacity_owner_id):
            if not reservation.accepting_allocations or not reservation.allocation_shape.can_host(
                request
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
    allocation_owners: CapacityAllocationOwnerDirectory | None = None

    @contextmanager
    def mutation_lock(self, capacity_owner_id: str) -> Iterator[None]:
        with self.reservations.mutation_lock(capacity_owner_id):
            yield

    def can_acquire(self, request: SchedulerWorkerRequest) -> bool:
        return self._controller_for_request(request) is not None

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
        last_result: CapacityAcquisitionResult | None = None
        contention: CapacityReservationConflictError | None = None
        for index, controller in enumerate(controllers):
            remaining = controllers[index + 1 :]
            try:
                result = self._acquire_from_controller(
                    request,
                    controller,
                    now=current_time,
                )
            except CapacityReservationConflictError as exc:
                # Another scheduler holds this unit's mutation lease. Trying the
                # next unit is worth doing, but the contention has to survive the
                # loop: if no unit serves the request, the caller requeues on
                # this signal rather than being told no capacity exists.
                contention = contention or exc
                continue
            except Exception:
                continue
            last_result = result
            if result.status in {
                CapacityAcquisitionStatus.ExistingPending,
                CapacityAcquisitionStatus.Requested,
            }:
                return result
            if remaining:
                # Only what we are abandoning. With no candidate left the claim
                # is the answer: releasing it here would mint a fresh one on the
                # next attempt and churn reservations for as long as the pool
                # stays full, instead of holding one and waiting for it to drain.
                self._release_failed_failover_allocation(result, request, now=current_time)
        if last_result is not None:
            return last_result
        if contention is not None:
            raise contention
        return _unsupported_result(
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
                    desired_unit=releasing.desired_unit,
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
            result = controller.ensure_capacity(
                reservation,
                owner_reservations=owner_reservations,
                now=now,
            )
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
        """Bind a pending reservation to its registered worker before dispatch."""

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
                        "terminal_reason": None,
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
                    if reservation.status is CapacityReservationStatus.Failed and allocations:
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
                                        "terminal_reason": None,
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
                                        "status": CapacityReservationStatus.Failed,
                                        "acquisition_created": False,
                                        "release_requested": False,
                                        "terminal_reason": (
                                            CapacityTerminalReason.RegistrationDeadlineExpired
                                        ),
                                    }
                                ),
                                expected_resource_version=reservation.resource_version,
                                now=current_time,
                            )
                        else:
                            reservation = self.reservations.update(
                                _unconfirmed_release(reservation, release_result),
                                expected_resource_version=reservation.resource_version,
                                now=current_time,
                            )
                        reconciled.append(reservation)
                        continue
                    if controller is None:
                        reconciled.append(reservation)
                        continue
                    result = controller.ensure_capacity(
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
                        "terminal_reason": None,
                    }
                ),
                expected_resource_version=reservation.resource_version,
                now=now,
            )
            return self.reservations.release_terminal(registered.id, now=now) or registered
        if reservation.acquisition_created:
            controller = self._controller_for_owner(reservation.capacity_owner_id)
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
                    _unconfirmed_release(reservation, release_result),
                    expected_resource_version=reservation.resource_version,
                    now=now,
                )
            reservation = self.reservations.update(
                reservation.model_copy(
                    update={
                        "acquisition_created": False,
                        "release_requested": False,
                        "terminal_reason": None,
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
            reservation = self.reservations.prepare_release(reservation, now=now)
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
            CapacityAcquisitionStatus.ExistingPending: CapacityReservationStatus.Pending,
            CapacityAcquisitionStatus.Requested: CapacityReservationStatus.Pending,
            # Full is backpressure, not failure: the reservation keeps waiting
            # and the pool keeps its place in selection.
            CapacityAcquisitionStatus.AtLimit: reservation.status,
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
                    "target_machine_id": result.target_machine_id or reservation.target_machine_id,
                    "terminal_reason": (
                        CapacityTerminalReason.AcquisitionUnsupported
                        if status is CapacityReservationStatus.Failed
                        else None
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


def _release_reason(status: CapacityReservationStatus) -> CapacityTerminalReason:
    if status is CapacityReservationStatus.Registered:
        return CapacityTerminalReason.ReleasedAfterRegistration
    return CapacityTerminalReason.ReleasedBeforeRegistration


def _unconfirmed_release(
    reservation: CapacityProvisioningReservation,
    result: CapacityAcquisitionResult,
) -> CapacityProvisioningReservation:
    # The provider's own wording is the only account of why the unit is still
    # held, and the durable record keeps a classification rather than that text.
    LOGGER.warning(
        "capacity release for reservation %s is unconfirmed: %s",
        reservation.id,
        result.reason,
    )
    return reservation.model_copy(
        update={"terminal_reason": CapacityTerminalReason.ReleaseUnconfirmed}
    )


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
    "CapacityReservationStateTransitionError",
    "CapacityReservationStatus",
    "CapacityReservationVersionConflictError",
    "CapacityTerminalReason",
    "CapacityWorkerRepository",
    "ComputePoolCapacityController",
    "RedisCapacityReservationRepository",
    "reservation_matches_worker",
    "reservation_shape_for_request",
]
