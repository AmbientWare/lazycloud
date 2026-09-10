from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Barrier
from time import sleep

import pytest
from compute.capacity_errors import (
    CapacityReservationConflictError,
    CapacityReservationLockContendedError,
)
from compute.request_placement import ComputeCapacityPurchase
from coordination.redis_client import AsyncRedisClient
from scheduler.capacity_reservations import (
    CapacityAcquisitionResult,
    CapacityAcquisitionStatus,
    CapacityProvisioningReservation,
    CapacityRequestShape,
    CapacityReservationDecision,
    CapacityReservationService,
    CapacityReservationStateTransitionError,
    CapacityReservationStatus,
    CapacityTerminalReason,
    ComputeUnitCapacityController,
    RedisCapacityReservationRepository,
    reservation_shape_for_request,
)
from scheduler.containers import (
    SchedulerContainerDispatchStatus,
    SchedulerContainerRequestService,
)
from scheduler.pool_sizing import (
    CapacityPoolOperationalHealth,
    WorkerPoolEffectiveHeadroom,
    WorkerPoolSizingAction,
    WorkerPoolSizingAllocation,
    WorkerPoolSizingPlan,
    WorkerPoolSizingReservation,
)
from scheduler.preemption import SchedulerGpuBackfillPreemptionService
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
)
from shared.billing_quotes import ContainerShape
from shared.capacity import CapacityAcquisitionRequest as ComputeCapacityRequest
from shared.capacity import CapacityAcquisitionResult as ComputeCapacityResult
from shared.capacity import (
    CapacityOwnerKind,
    CapacityOwnerSource,
    CapacityPoolSizingSnapshot,
    CapacityReleaseRequest,
)
from shared.compute_policy import (
    ComputeUnitRecord,
    MachinePool,
    UnitName,
)
from shared.container_requests import StopContainerReason
from shared.errors import UpstreamUnavailableError
from shared.realtime.contracts import CloudEventRecord, EventDataInput, EventRecordType
from shared.scheduling import (
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    SchedulerWorkerStatus,
    WorkerUnavailableReason,
)
from tests.real_redis import RealRedisActors

OWNER_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
OTHER_OWNER_ID = "22222222-2222-4222-8222-222222222222"
DEFAULT_UNIT_NAME = UnitName("default")
DEFAULT_POOL = MachinePool("default")


def _request(container_id: str, *, cpu: int = 1_000) -> SchedulerWorkerRequest:
    return SchedulerWorkerRequest(
        workspace_id="workspace-1",
        stub_id="stub-1",
        container_id=container_id,
        cpu_millicores=cpu,
        memory_mib=512,
        pool_selector="default",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _shape() -> CapacityRequestShape:
    return CapacityRequestShape(
        cpu_millicores=4_000,
        memory_mib=8_192,
        runtime_classes=("runsc",),
    )


@dataclass(slots=True)
class _AllocationOwners:
    active: set[tuple[str, str]]

    def is_active(self, *, workspace_id: str, container_id: str) -> bool:
        return (workspace_id, container_id) in self.active


def _repository(real_redis_actors: RealRedisActors) -> RedisCapacityReservationRepository:
    return RedisCapacityReservationRepository(real_redis_actors.client())


@dataclass(slots=True)
class _Controller:
    capacity_owner_id: str = OWNER_ID
    owner_kind: CapacityOwnerKind = CapacityOwnerKind.PooledProvider
    unit_name: UnitName = DEFAULT_UNIT_NAME
    pool: MachinePool = DEFAULT_POOL
    registration_timeout: timedelta = timedelta(minutes=10)
    ensure_calls: list[str] = field(default_factory=list)
    release_calls: list[str] = field(default_factory=list)
    ensure_status: CapacityAcquisitionStatus = CapacityAcquisitionStatus.Requested
    release_status: CapacityAcquisitionStatus = CapacityAcquisitionStatus.ExistingPending
    failures_remaining: int = 0
    delay_seconds: float = 0
    priority: int = 0
    hourly_cost_micros: int | None = None
    health: CapacityPoolOperationalHealth = CapacityPoolOperationalHealth.Healthy
    target_machine_id: str = ""
    owns_capacity: bool = False
    default_eligible: bool = True

    def operational_health(self, *, now: datetime) -> CapacityPoolOperationalHealth:
        _ = now
        return self.health

    def reconcile_sizing(
        self,
        *,
        reservations: Sequence[WorkerPoolSizingReservation],
        allocations: Sequence[WorkerPoolSizingAllocation],
        now: datetime,
    ) -> WorkerPoolSizingPlan:
        _ = reservations, allocations, now
        return WorkerPoolSizingPlan(
            action=WorkerPoolSizingAction.None_,
            capacity_owner_id=self.capacity_owner_id,
            pool=self.pool,
            current_units=0,
            target_units=0,
            headroom=WorkerPoolEffectiveHeadroom(),
            initial_target_reached=True,
            reason="test controller has no proactive sizing",
        )

    def accepts(self, request: SchedulerWorkerRequest) -> bool:
        if request.pool_selector:
            return request.pool_selector == self.pool
        return self.default_eligible

    def reservation_shape(self, request: SchedulerWorkerRequest) -> CapacityRequestShape:
        return reservation_shape_for_request(
            request,
            worker_cpu_millicores=4_000,
            worker_memory_mib=8_192,
            worker_gpu_type="",
            worker_gpu_count=0,
            worker_runtimes=("runsc",),
            worker_preemptible=False,
        )

    def ensure_capacity(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        owner_reservations: tuple[CapacityProvisioningReservation, ...],
        now: datetime,
    ) -> CapacityAcquisitionResult:
        _ = owner_reservations, now
        if self.delay_seconds:
            sleep(self.delay_seconds)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise OSError("capacity boundary unavailable")
        # Compute creates the operation row on the first call and finds it on
        # every later one, so a repeat for the same reservation buys nothing.
        repeat = reservation.id in self.ensure_calls
        self.ensure_calls.append(reservation.id)
        if repeat and self.ensure_status is CapacityAcquisitionStatus.Requested:
            return self._result(reservation, CapacityAcquisitionStatus.ExistingPending)
        return self._result(reservation, self.ensure_status)

    def plan_release(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        owner_reservations: tuple[CapacityProvisioningReservation, ...],
        now: datetime,
    ) -> CapacityAcquisitionResult:
        _ = owner_reservations, now
        return self._result(reservation, CapacityAcquisitionStatus.ExistingPending)

    def release(
        self,
        reservation: CapacityProvisioningReservation,
        *,
        owner_reservations: tuple[CapacityProvisioningReservation, ...],
        now: datetime,
    ) -> CapacityAcquisitionResult:
        _ = owner_reservations, now
        self.release_calls.append(reservation.id)
        return self._result(reservation, self.release_status)

    def _result(
        self,
        reservation: CapacityProvisioningReservation,
        status: CapacityAcquisitionStatus,
    ) -> CapacityAcquisitionResult:
        return CapacityAcquisitionResult(
            status=status,
            capacity_owner_id=reservation.capacity_owner_id,
            reservation_id=reservation.id,
            operation_id=reservation.operation_id,
            desired_unit=1,
            owns_capacity=self.owns_capacity,
            target_machine_id=self.target_machine_id,
        )


@dataclass(slots=True)
class _WorkerRepository:
    workers: list[SchedulerWorkerRecord] = field(default_factory=list)

    def list_workers(self) -> list[SchedulerWorkerRecord]:
        return list(self.workers)


@dataclass(slots=True)
class _SizingSnapshots:
    pool: ComputeUnitRecord

    def pool_sizing_snapshot(self, capacity_owner_id: str) -> CapacityPoolSizingSnapshot:
        if capacity_owner_id != self.pool.capacity_owner_id:
            raise ValueError("unknown capacity owner")
        return CapacityPoolSizingSnapshot(capacity_owner_id=capacity_owner_id)


@dataclass(slots=True)
class _UnusedComputeCapacity(_SizingSnapshots):
    def ensure_capacity(
        self,
        request: ComputeCapacityRequest,
        *,
        minimum_unit: int = 0,
    ) -> ComputeCapacityResult:
        _ = minimum_unit
        raise AssertionError(f"unexpected capacity acquisition: {request.operation_id}")

    def release_acquired_capacity(
        self,
        request: CapacityReleaseRequest,
    ) -> ComputeCapacityResult:
        raise AssertionError(f"unexpected capacity release: {request.operation_id}")


def _managed_pool() -> ComputeUnitRecord:
    return ComputeUnitRecord(
        id=OWNER_ID,
        workspace_id=WORKSPACE_ID,
        name=UnitName("default"),
        pool=MachinePool("default"),
        provider="generic",
        capacity_owner_id=OWNER_ID,
        capacity_owner_kind=CapacityOwnerKind.PooledProvider,
        capacity_owner_source=CapacityOwnerSource.Provider,
        max_machines=2,
        scaling_enabled=True,
        default_eligible=True,
        worker_cpu_millicores=4_000,
        worker_memory_mib=8_192,
    )


def _purchases(service: CapacityReservationService) -> tuple[ComputeCapacityPurchase, ...]:
    return tuple(
        ComputeCapacityPurchase(item.capacity_owner_id, lambda: None)
        for item in service.controllers()
    )


class _IdentityPlacement:
    def place(self, request: SchedulerWorkerRequest) -> SchedulerWorkerRequest:
        return request

    def purchase_candidates(
        self, request: SchedulerWorkerRequest
    ) -> tuple[ComputeCapacityPurchase, ...]:
        return (ComputeCapacityPurchase(OWNER_ID, lambda: None),)


class _FailureHandler:
    def mark_scheduling_failed(
        self,
        request: SchedulerWorkerRequest,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> None:
        _ = request, reason, now


class _Assignments:
    def assign_runtime(
        self,
        *,
        container_id: str,
        workspace_id: str,
        runtime_worker_id: str,
        runtime_machine_id: str,
        compute_worker_id: str | None = None,
        compute_machine_id: str | None = None,
        shape: ContainerShape | None = None,
    ) -> None:
        _ = (
            container_id,
            workspace_id,
            runtime_worker_id,
            runtime_machine_id,
            compute_worker_id,
            compute_machine_id,
        )

    def clear_runtime_assignment(
        self,
        *,
        container_id: str,
        runtime_worker_id: str,
    ) -> None:
        _ = container_id, runtime_worker_id


class _Wake:
    def signal(self) -> bool:
        return True


class _UnownedWorkspaces:
    """No workspace here has an account behind it.

    These cases are about reservation accounting, and every worker they build is
    shared capacity, which placement decides without asking who owns the request.
    """

    def owner_user_id(self, workspace_id: str) -> str:
        _ = workspace_id
        return ""


class _Events:
    def append_event(
        self,
        event_type: str | EventRecordType,
        data: EventDataInput,
        *,
        event_id: str | None = None,
    ) -> CloudEventRecord:
        _ = event_type, data, event_id
        raise RuntimeError("event sink is intentionally unavailable")


def test_reservation_is_idempotent_per_request_and_reuses_compatible_capacity(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    with repository.mutation_lock(OWNER_ID):
        first = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool=MachinePool("default"),
            owner_kind=CapacityOwnerKind.PooledProvider,
            request=_request("container-1"),
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
        repeated = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool=MachinePool("default"),
            owner_kind=CapacityOwnerKind.PooledProvider,
            request=_request("container-1"),
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
        second = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool=MachinePool("default"),
            owner_kind=CapacityOwnerKind.PooledProvider,
            request=_request("container-2"),
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )

    assert first.created
    assert not repeated.created
    assert not second.created
    assert first.reservation.id == repeated.reservation.id == second.reservation.id
    assert [item.container_id for item in repository.allocations_for(first.reservation.id)] == [
        "container-1",
        "container-2",
    ]


def test_reservation_capacity_and_owner_identity_prevent_false_reuse(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with repository.mutation_lock(OWNER_ID):
        first = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool=MachinePool("shared-name"),
            owner_kind=CapacityOwnerKind.PooledProvider,
            request=_request("container-1", cpu=3_000),
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
        second = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool=MachinePool("shared-name"),
            owner_kind=CapacityOwnerKind.PooledProvider,
            request=_request("container-2", cpu=3_000),
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
    other_request = _request("container-3").model_copy(update={"capacity_owner_id": OTHER_OWNER_ID})
    with repository.mutation_lock(OTHER_OWNER_ID):
        other = repository.reserve(
            capacity_owner_id=OTHER_OWNER_ID,
            pool=MachinePool("shared-name"),
            owner_kind=CapacityOwnerKind.PooledProvider,
            request=other_request,
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )

    assert first.reservation.id != second.reservation.id
    assert other.reservation.id not in {first.reservation.id, second.reservation.id}


def test_one_container_cannot_reserve_two_capacity_owners(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    request = _request("competing-owners")
    barrier = Barrier(2)

    def reserve(owner_id: str) -> CapacityReservationDecision | CapacityReservationConflictError:
        with repository.mutation_lock(owner_id):
            barrier.wait()
            try:
                return repository.reserve(
                    capacity_owner_id=owner_id,
                    pool=DEFAULT_POOL,
                    owner_kind=CapacityOwnerKind.PooledProvider,
                    request=request,
                    shape=_shape(),
                    registration_timeout=timedelta(minutes=10),
                )
            except CapacityReservationConflictError as exc:
                return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, (OWNER_ID, OTHER_OWNER_ID)))
    assert sum(isinstance(item, CapacityReservationDecision) for item in results) == 1
    assert sum(isinstance(item, CapacityReservationConflictError) for item in results) == 1
    [reservation] = repository.list_all()
    [allocation] = repository.allocations_for(reservation.id)
    assert allocation.container_id == request.container_id
    assert not repository.release_allocation(
        request.container_id, expected_reservation_id="different-reservation"
    )
    assert repository.allocation_for_request(request.container_id) == allocation


def test_pending_capacity_is_reused_before_supplier_discovery(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller()
    controllers = [controller]
    service = CapacityReservationService(repository, lambda: controllers)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    first = service.acquire(_request("container-1"), purchases=lambda: _purchases(service), now=now)
    other = _Controller(capacity_owner_id=OTHER_OWNER_ID, priority=100)
    controllers.insert(0, other)

    def unavailable() -> tuple[ComputeCapacityPurchase, ...]:
        raise UpstreamUnavailableError("supplier catalog unavailable")

    resumed = service.acquire(_request("container-1"), purchases=unavailable, now=now)
    second = service.acquire(_request("container-2"), purchases=unavailable, now=now)

    assert first.status is CapacityAcquisitionStatus.Requested
    assert second.status is CapacityAcquisitionStatus.ExistingPending
    assert second.reservation_id == first.reservation_id
    assert resumed.reservation_id == first.reservation_id
    assert other.ensure_calls == []
    assert set(controller.ensure_calls) == {first.reservation_id}


def test_terminal_retry_releases_stale_reservation_before_new_attempt(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller(ensure_status=CapacityAcquisitionStatus.Unsupported)
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)

    first = service.acquire(
        _request("container-terminal"), purchases=lambda: _purchases(service), now=now
    )
    second = service.acquire(
        _request("container-terminal"),
        purchases=lambda: _purchases(service),
        now=now + timedelta(seconds=1),
    )

    first_reservation = repository.get(first.reservation_id)
    assert first.status is CapacityAcquisitionStatus.Unsupported
    assert second.status is CapacityAcquisitionStatus.Unsupported
    assert first.reservation_id != second.reservation_id
    assert first_reservation is not None
    assert first_reservation.status is CapacityReservationStatus.Released


def test_a_full_pool_keeps_one_open_claim_instead_of_churning_released_ones(
    real_redis_actors: RealRedisActors,
) -> None:
    """Full is backpressure: the container's claim waits for the pool to drain.

    When at-limit was terminal, every attempt released the reservation and
    minted a fresh one, so a busy workspace ground through reservation churn
    for as long as the pool stayed full.
    """
    repository = _repository(real_redis_actors)
    controller = _Controller(ensure_status=CapacityAcquisitionStatus.AtLimit)
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)

    first = service.acquire(
        _request("container-at-limit"), purchases=lambda: _purchases(service), now=now
    )
    second = service.acquire(
        _request("container-at-limit"),
        purchases=lambda: _purchases(service),
        now=now + timedelta(seconds=1),
    )

    assert first.status is CapacityAcquisitionStatus.AtLimit
    assert second.status is CapacityAcquisitionStatus.AtLimit
    assert first.reservation_id == second.reservation_id
    retained = repository.get(first.reservation_id)
    assert retained is not None
    assert retained.open


def test_unpinned_acquisition_fails_over_from_at_limit_pool_in_priority_order(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    primary = _Controller(
        ensure_status=CapacityAcquisitionStatus.AtLimit,
        priority=20,
    )
    fallback = _Controller(
        capacity_owner_id=OTHER_OWNER_ID,
        unit_name=UnitName("default"),
        priority=10,
    )
    service = CapacityReservationService(repository, lambda: [fallback, primary])
    request = _request("failover")

    result = service.acquire(
        request, purchases=lambda: _purchases(service), now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    assert result.status is CapacityAcquisitionStatus.Requested
    assert result.capacity_owner_id == OTHER_OWNER_ID
    assert len(primary.ensure_calls) == 1
    assert len(fallback.ensure_calls) == 1
    primary_reservation = repository.get(primary.ensure_calls[0])
    assert primary_reservation is not None
    assert primary_reservation.status is CapacityReservationStatus.Released


def test_rejected_acquisition_reassigns_requests_and_retries_owned_cleanup(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    primary = _Controller(priority=20)
    fallback = _Controller(capacity_owner_id=OTHER_OWNER_ID)
    service = CapacityReservationService(repository, lambda: [primary, fallback])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = _request("rejected")
    acquired = service.acquire(request, purchases=lambda: _purchases(service), now=now)
    primary.ensure_status = CapacityAcquisitionStatus.Rejected
    primary.release_status = CapacityAcquisitionStatus.TemporarilyUnavailable

    service.reconcile([], now=now + timedelta(seconds=1))

    failed = repository.get(acquired.reservation_id)
    assert failed is not None
    assert failed.status is CapacityReservationStatus.Failed
    assert failed.acquisition_created and failed.release_requested
    assert repository.allocation_for_request(request.container_id) is None
    assert repository.release_terminal(failed.id, now=now) == failed

    moved = service.acquire(
        request,
        purchases=lambda: tuple(
            candidate
            for candidate in _purchases(service)
            if candidate.capacity_owner_id == OTHER_OWNER_ID
        ),
        now=now + timedelta(seconds=2),
    )
    assert moved.capacity_owner_id == OTHER_OWNER_ID
    late_worker = _worker(OWNER_ID, created_at=now + timedelta(seconds=3))
    primary.release_status = CapacityAcquisitionStatus.ExistingPending

    service.reconcile([late_worker], now=now + timedelta(seconds=4))

    released = repository.get(acquired.reservation_id)
    assert released is not None
    assert released.status is CapacityReservationStatus.Released
    assert not released.acquisition_created
    assert not released.release_requested
    allocation = repository.allocation_for_request(request.container_id)
    assert allocation is not None and allocation.reservation_id == moved.reservation_id
    assert fallback.release_calls == []


def test_rejected_first_response_retains_provider_reported_ownership_until_cleanup(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller(
        ensure_status=CapacityAcquisitionStatus.Rejected,
        owns_capacity=True,
    )
    service = CapacityReservationService(repository, lambda: [controller])
    acquired = service.acquire(
        _request("recovered-rejection"),
        purchases=lambda: _purchases(service),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    reservation = repository.get(acquired.reservation_id)
    assert reservation is not None
    assert controller.release_calls == [reservation.id]
    assert reservation.status is CapacityReservationStatus.Released
    assert reservation.terminal_reason is CapacityTerminalReason.AcquisitionRejected
    assert not reservation.acquisition_created


def test_fixed_pool_rejects_cross_workspace_and_oversized_capacity_requests() -> None:
    compute = ComputeUnitCapacityController(
        "workspace-1",
        _managed_pool(),
        _UnusedComputeCapacity(_managed_pool()),
        _WorkerRepository(),
    )
    assert compute.accepts(_request("fits"))
    assert not compute.accepts(
        _request("wrong-workspace").model_copy(update={"workspace_id": "workspace-2"})
    )
    assert not compute.accepts(_request("oversized", cpu=4_001))
    # A unit only ever serves the pool the request named. Failing over to a unit
    # of another pool would run tenant work on a fleet nobody asked for, and this
    # filter is the only thing standing between a request and that fleet.
    assert not compute.accepts(
        _request("other-pool").model_copy(update={"pool_selector": "another-pool"})
    )


def test_platform_capacity_accepts_customer_cold_requests_in_the_requested_market() -> None:
    unit = _managed_pool().model_copy(update={"platform_fleet": True})
    controller = ComputeUnitCapacityController(
        unit.workspace_id, unit, _UnusedComputeCapacity(unit), _WorkerRepository()
    )
    request = _request("customer-cold-capacity").model_copy(update={"preemptible": False})

    assert request.workspace_id != unit.workspace_id
    assert controller.accepts(request)
    assert not controller.accepts(request.model_copy(update={"pool_selector": "other-fleet"}))

    controller.unit = unit.model_copy(update={"worker_preemptible": True})
    assert not controller.accepts(request)
    assert controller.accepts(request.model_copy(update={"preemptible": True}))


def test_placement_miss_transfers_capacity_to_dispatch_before_reconciliation(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    controller = _Controller()
    capacity = CapacityReservationService(
        RedisCapacityReservationRepository(redis),
        lambda: [controller],
    )
    requests = SchedulerContainerRequestService(
        workers=workers,
        containers=containers,
        placement=_IdentityPlacement(),
        failure_handler=_FailureHandler(),
        assignments=_Assignments(),
        dispatch_wake=_Wake(),
        lifecycle_events=_Events(),
        capacity_reservations=capacity,
        workspace_owners=_UnownedWorkspaces(),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = _request("container-e2e")

    assert requests.submit(request, ready_at=now).accepted
    [waiting] = requests.dispatch_ready(now=now, limit=1)

    assert waiting.status is SchedulerContainerDispatchStatus.Waiting
    assert len(controller.ensure_calls) == 1

    worker = _worker(OWNER_ID, created_at=now + timedelta(milliseconds=500))
    workers.add_worker(worker, now=worker.created_at)
    workers.toggle_worker_available(worker.worker_id, now=worker.created_at)
    [reservation] = capacity.reservations.list_all()

    [dispatched] = requests.dispatch_ready(now=now + timedelta(seconds=1), limit=1)
    capacity.reconcile(workers.list_workers(), now=now + timedelta(seconds=2))

    assert dispatched.status is SchedulerContainerDispatchStatus.Dispatched
    assert dispatched.worker_id == worker.worker_id
    assert capacity.reservations.allocation_for_request(request.container_id) is None
    reservation = capacity.reservations.get(reservation.id)
    assert reservation is not None
    assert reservation.status is CapacityReservationStatus.Released
    assert reservation.target_worker_id == worker.worker_id
    assert len(controller.ensure_calls) == 1
    updated = workers.get_worker(worker.worker_id)
    assert updated is not None
    assert updated.free_cpu_millicores == 3_000


@pytest.mark.parametrize("cpu_pending", [False, True])
def test_ready_gpu_backfill_dispatches_without_supplier_acquisition(
    real_redis_actors: RealRedisActors,
    cpu_pending: bool,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    now = datetime.now(UTC)
    gpu = workers.add_worker(
        _worker(OWNER_ID, created_at=now).model_copy(
            update={"gpu_type": "L4", "total_gpu_count": 1, "free_gpu_count": 0}
        ),
        now=now,
    )
    cpu = None
    if cpu_pending:
        cpu = workers.add_worker(
            _worker(OTHER_OWNER_ID, created_at=now).model_copy(
                update={"status": SchedulerWorkerStatus.Pending}
            ),
            now=now,
        )

    def unavailable_controllers() -> Sequence[ComputeUnitCapacityController]:
        raise UpstreamUnavailableError("supplier inventory unavailable")

    class Stopper:
        def stop(self, container_id: str, *, reason: StopContainerReason) -> None:
            containers.cancel_container_request(container_id)

    capacity = CapacityReservationService(
        RedisCapacityReservationRepository(redis), unavailable_controllers
    )
    requests = SchedulerContainerRequestService(
        workers=workers,
        containers=containers,
        placement=_IdentityPlacement(),
        failure_handler=_FailureHandler(),
        assignments=_Assignments(),
        dispatch_wake=_Wake(),
        lifecycle_events=_Events(),
        workspace_owners=_UnownedWorkspaces(),
        capacity_reservations=capacity,
        backfill_preemption=SchedulerGpuBackfillPreemptionService(workers, containers, Stopper()),
    )
    request = _request("backfill-before-capacity").model_copy(
        update={"preemptible": True, "timestamp": now}
    )
    assert requests.submit(request, ready_at=now).accepted
    if cpu_pending:
        assert requests.submit(
            _request("ordinary-cpu", cpu=4_000).model_copy(update={"timestamp": now}), ready_at=now
        ).accepted

    outcomes = requests.dispatch_ready(now=now, limit=2)
    result = next(item for item in outcomes if item.container_id == request.container_id)

    assert result.status is SchedulerContainerDispatchStatus.Dispatched
    assert result.worker_id == gpu.worker_id
    placed = containers.get_container_state(request.container_id)
    assert placed is not None and placed.backfill and placed.preemptible
    assert capacity.reservations.list_all() == []
    remaining = workers.get_worker(gpu.worker_id)
    assert remaining is not None and remaining.free_cpu_millicores == 3_000
    if cpu_pending:
        ordinary = next(item for item in outcomes if item.container_id == "ordinary-cpu")
        assert ordinary.status is SchedulerContainerDispatchStatus.Waiting
        assert cpu is not None
        assert ordinary.worker_id == cpu.worker_id


@pytest.mark.parametrize("shared_cpu_reservation", [False, True])
def test_gpu_backfill_releases_only_its_pending_cpu_allocation(
    real_redis_actors: RealRedisActors,
    shared_cpu_reservation: bool,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    repository = RedisCapacityReservationRepository(redis)
    controller = _Controller(release_status=CapacityAcquisitionStatus.TemporarilyUnavailable)
    capacity = CapacityReservationService(repository, lambda: [controller])
    now = datetime.now(UTC)
    gpu = workers.add_worker(
        _worker(OTHER_OWNER_ID, created_at=now).model_copy(
            update={"gpu_type": "L4", "total_gpu_count": 1, "free_gpu_count": 0}
        ),
        now=now,
    )

    class Stopper:
        def stop(self, container_id: str, *, reason: StopContainerReason) -> None:
            containers.cancel_container_request(container_id)

    requests = SchedulerContainerRequestService(
        workers=workers,
        containers=containers,
        placement=_IdentityPlacement(),
        failure_handler=_FailureHandler(),
        assignments=_Assignments(),
        dispatch_wake=_Wake(),
        lifecycle_events=_Events(),
        workspace_owners=_UnownedWorkspaces(),
        capacity_reservations=capacity,
        backfill_preemption=SchedulerGpuBackfillPreemptionService(workers, containers, Stopper()),
    )
    request = _request("move-to-gpu").model_copy(update={"preemptible": True})
    assert requests.submit(request, ready_at=now).accepted
    acquired = capacity.acquire(request, purchases=lambda: _purchases(capacity), now=now)
    if shared_cpu_reservation:
        sibling = capacity.acquire(
            _request("keep-cpu"), purchases=lambda: _purchases(capacity), now=now
        )
        assert sibling.reservation_id == acquired.reservation_id

    [result] = requests.dispatch_ready(now=now)

    assert result.status is SchedulerContainerDispatchStatus.Dispatched
    assert result.worker_id == gpu.worker_id
    assert repository.allocation_for_request(request.container_id) is None
    reservation = repository.get(acquired.reservation_id)
    assert reservation is not None and reservation.open
    if shared_cpu_reservation:
        assert {a.container_id for a in repository.allocations_for(reservation.id)} == {"keep-cpu"}
        assert not reservation.release_requested
    else:
        assert repository.allocations_for(reservation.id) == []
        assert reservation.release_requested and reservation.acquisition_created


def test_registered_gpu_reservation_recovers_cpu_backfill_before_dispatch(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    repository = RedisCapacityReservationRepository(redis)
    capacity = CapacityReservationService(repository, tuple)
    now = datetime.now(UTC)
    worker = workers.add_worker(
        _worker(OWNER_ID, created_at=now).model_copy(
            update={
                "status": SchedulerWorkerStatus.Available,
                "gpu_type": "L4",
                "total_gpu_count": 1,
                "free_gpu_count": 1,
                "free_cpu_millicores": 0,
            }
        ),
        now=now,
    )
    containers.set_container_state(
        SchedulerContainerState(
            container_id="cpu-backfill",
            stub_id="stub-1",
            workspace_id="workspace-1",
            worker_id=worker.worker_id,
            status=SchedulerContainerStatus.Running,
            backfill=True,
            preemptible=True,
            cpu_millicores=worker.total_cpu_millicores,
            memory_mib=512,
        )
    )

    class Stopper:
        def stop(self, container_id: str, *, reason: StopContainerReason) -> None:
            assert reason is StopContainerReason.Preempted
            containers.cancel_container_request(container_id)

    requests = SchedulerContainerRequestService(
        workers=workers,
        containers=containers,
        placement=_IdentityPlacement(),
        failure_handler=_FailureHandler(),
        assignments=_Assignments(),
        dispatch_wake=_Wake(),
        lifecycle_events=_Events(),
        workspace_owners=_UnownedWorkspaces(),
        capacity_reservations=capacity,
        backfill_preemption=SchedulerGpuBackfillPreemptionService(workers, containers, Stopper()),
    )
    request = _request("reserved-gpu").model_copy(update={"gpu": ["L4"], "gpu_count": 1})
    assert requests.submit(request, ready_at=now).accepted
    with repository.mutation_lock(OWNER_ID):
        repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool=worker.pool,
            owner_kind=CapacityOwnerKind.PooledProvider,
            request=request,
            shape=_shape().model_copy(update={"gpu_type": "L4", "gpu_count": 1}),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
        capacity.prepare_dispatch(request.container_id, worker, now=now)

    [waiting] = requests.dispatch_ready(now=now)
    assert waiting.status is SchedulerContainerDispatchStatus.Waiting
    backfill = containers.get_container_state("cpu-backfill")
    assert backfill is not None and backfill.backfill_eviction_requested
    assert backfill.status is SchedulerContainerStatus.Stopping
    assert capacity.registered_worker_id(request.container_id) == worker.worker_id
    assert workers.has_recoverable_container_request(request.container_id)

    containers.update_container_status("cpu-backfill", SchedulerContainerStatus.Complete)
    workers.toggle_worker_available(worker.worker_id, now=now + timedelta(seconds=1))
    [dispatched] = requests.dispatch_ready(now=now + timedelta(seconds=2))
    assert dispatched.status is SchedulerContainerDispatchStatus.Dispatched
    assert dispatched.worker_id == worker.worker_id
    assert repository.allocation_for_request(request.container_id) is None


def test_provider_reconciliation_does_not_block_final_dispatch(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    capacity = CapacityReservationService(
        RedisCapacityReservationRepository(redis),
        tuple,
    )
    requests = SchedulerContainerRequestService(
        workers=workers,
        containers=containers,
        placement=_IdentityPlacement(),
        failure_handler=_FailureHandler(),
        assignments=_Assignments(),
        dispatch_wake=_Wake(),
        lifecycle_events=_Events(),
        capacity_reservations=capacity,
        workspace_owners=_UnownedWorkspaces(),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = _request("container-provider-reconcile")
    worker = _worker(OWNER_ID, created_at=now)
    workers.add_worker(worker, now=now)
    workers.toggle_worker_available(worker.worker_id, now=now)
    assert requests.submit(request, ready_at=now).accepted

    mutation_started = Barrier(2)
    release_mutation = Barrier(2)

    def reconcile_provider() -> None:
        with capacity.mutation_lock(OWNER_ID):
            mutation_started.wait()
            release_mutation.wait()

    with ThreadPoolExecutor(max_workers=1) as executor:
        reconciling = executor.submit(reconcile_provider)
        mutation_started.wait()
        [result] = requests.dispatch_ready(now=now + timedelta(seconds=1), limit=1)
        release_mutation.wait()
        reconciling.result()

    assert result.status is SchedulerContainerDispatchStatus.Dispatched
    assert result.worker_id == worker.worker_id


@pytest.mark.anyio
async def test_final_dispatch_rechecks_owner_worker_after_scale_zero_mutation(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    capacity = CapacityReservationService(
        RedisCapacityReservationRepository(redis),
        tuple,
    )
    requests = SchedulerContainerRequestService(
        workers=workers,
        containers=containers,
        placement=_IdentityPlacement(),
        failure_handler=_FailureHandler(),
        assignments=_Assignments(),
        dispatch_wake=_Wake(),
        lifecycle_events=_Events(),
        capacity_reservations=capacity,
        workspace_owners=_UnownedWorkspaces(),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = _request("container-scale-zero-fence")
    worker = _worker(OWNER_ID, created_at=now)
    workers.add_worker(worker, now=now)
    workers.toggle_worker_available(worker.worker_id, now=now)
    assert requests.submit(request, ready_at=now).accepted

    stale_snapshot_read = Barrier(2)
    scale_zero_complete = Barrier(2)
    original_list_workers = RedisSchedulerWorkerRepository.list_workers
    list_count = 0

    def list_workers_after_scale_zero(
        repository: RedisSchedulerWorkerRepository,
    ) -> list[SchedulerWorkerRecord]:
        nonlocal list_count
        current = original_list_workers(repository)
        list_count += 1
        if list_count == 2:
            stale_snapshot_read.wait()
            scale_zero_complete.wait()
        return current

    monkeypatch.setattr(
        RedisSchedulerWorkerRepository,
        "list_workers",
        list_workers_after_scale_zero,
    )

    def scale_zero() -> None:
        stale_snapshot_read.wait()
        with capacity.mutation_lock(OWNER_ID), capacity.dispatch_lock(OWNER_ID):
            workers.disable_worker(
                worker.worker_id,
                reason=WorkerUnavailableReason.MachineRetired,
                now=now + timedelta(milliseconds=1),
            )
        scale_zero_complete.wait()

    with ThreadPoolExecutor(max_workers=1) as executor:
        scaling = executor.submit(scale_zero)
        [result] = requests.dispatch_ready(now=now + timedelta(seconds=1), limit=1)
        scaling.result()

    assert result.status is SchedulerContainerDispatchStatus.Waiting
    assert result.reason == "capacity-owner worker changed before final dispatch"
    current_worker = workers.get_worker(worker.worker_id)
    assert current_worker is not None
    assert current_worker.status is SchedulerWorkerStatus.Unavailable
    assert (
        await workers.wait_for_next_container_request(
            async_redis,
            worker.worker_id,
            timeout_seconds=0.01,
        )
        is None
    )
    assert workers.has_recoverable_container_request(request.container_id)
    state = containers.get_container_state(request.container_id)
    assert state is not None
    assert state.worker_id == ""


def test_available_worker_registration_uses_reported_schedulable_capacity(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller(target_machine_id="machine-1")
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    acquired = service.acquire(
        _request("container-1"), purchases=lambda: _purchases(service), now=now
    )
    wrong_owner = _worker(OTHER_OWNER_ID, created_at=now + timedelta(seconds=1))

    service.reconcile([wrong_owner], now=now + timedelta(seconds=2))
    assert service.registered_worker_id("container-1") == ""

    insufficient = _worker(OWNER_ID, created_at=now + timedelta(seconds=1)).model_copy(
        update={"free_memory_mib": 500, "total_memory_mib": 500}
    )
    service.reconcile([insufficient], now=now + timedelta(seconds=3))
    assert service.registered_worker_id("container-1") == ""

    wrong_machine = _worker(OWNER_ID, created_at=now + timedelta(seconds=1)).model_copy(
        update={
            "machine_id": "machine-2",
            "free_memory_mib": 7_900,
            "total_memory_mib": 7_900,
        }
    )
    service.reconcile([wrong_machine], now=now + timedelta(seconds=3))
    assert service.registered_worker_id("container-1") == ""

    matching = _worker(OWNER_ID, created_at=now + timedelta(seconds=1)).model_copy(
        update={"free_memory_mib": 7_900, "total_memory_mib": 7_900}
    )
    reconciled = service.reconcile([matching], now=now + timedelta(seconds=3))

    assert reconciled[-1].status is CapacityReservationStatus.Registered
    assert reconciled[-1].acquisition_shape.memory_mib == 8_192
    assert reconciled[-1].schedulable_shape is not None
    assert reconciled[-1].schedulable_shape.memory_mib == 7_900
    assert service.registered_worker_id("container-1") == matching.worker_id
    assert acquired.reservation_id == reconciled[-1].id


def test_registration_proof_requires_the_reserved_preemptibility_class() -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    on_demand_worker = _worker(OWNER_ID, created_at=created_at)
    preemptible_worker = on_demand_worker.model_copy(update={"preemptible": True})

    assert _shape().worker_capabilities_match(on_demand_worker)
    assert not _shape().worker_capabilities_match(preemptible_worker)
    preemptible_shape = _shape().model_copy(update={"preemptible": True})
    assert preemptible_shape.worker_capabilities_match(preemptible_worker)
    assert not preemptible_shape.worker_capabilities_match(on_demand_worker)


def test_registration_expiry_calls_capacity_owner_release_and_records_failure(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller(registration_timeout=timedelta(seconds=30))
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    acquired = service.acquire(
        _request("container-1"), purchases=lambda: _purchases(service), now=now
    )

    reconciled = service.reconcile([], now=now + timedelta(seconds=31))

    assert controller.release_calls == [acquired.reservation_id]
    assert reconciled[-1].status is CapacityReservationStatus.Failed


def test_cancellation_releases_exact_owned_capacity_after_last_allocation(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller()
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    acquired = service.acquire(
        _request("container-cancelled"), purchases=lambda: _purchases(service), now=now
    )

    service.release_request(
        "container-cancelled",
        now=now + timedelta(seconds=1),
    )

    reservation = repository.get(acquired.reservation_id)
    assert controller.release_calls == [acquired.reservation_id]
    assert reservation is not None
    assert reservation.status is CapacityReservationStatus.Released


def test_cancellation_after_registration_keeps_capacity_for_idle_drain(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller()
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    acquired = service.acquire(
        _request("container-registered-cancel"), purchases=lambda: _purchases(service), now=now
    )
    worker = _worker(OWNER_ID, created_at=now + timedelta(milliseconds=100))

    service.release_request(
        "container-registered-cancel",
        workers=(worker,),
        now=now + timedelta(seconds=1),
    )

    reservation = repository.get(acquired.reservation_id)
    assert controller.release_calls == []
    assert reservation is not None
    assert reservation.status is CapacityReservationStatus.Released


def test_reconcile_prunes_allocations_after_durable_container_owners_finish(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller()
    owners = _AllocationOwners(
        {
            ("workspace-1", "container-finished"),
            ("workspace-1", "container-running"),
        }
    )
    service = CapacityReservationService(
        repository,
        lambda: [controller],
        allocation_owners=owners,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    finished = service.acquire(
        _request("container-finished"), purchases=lambda: _purchases(service), now=now
    )
    running = service.acquire(
        _request("container-running"), purchases=lambda: _purchases(service), now=now
    )
    assert finished.reservation_id == running.reservation_id
    worker = _worker(OWNER_ID, created_at=now + timedelta(milliseconds=100))
    service.reconcile([worker], now=now + timedelta(seconds=1))

    owners.active.remove(("workspace-1", "container-finished"))
    service.reconcile([worker], now=now + timedelta(seconds=2))

    assert repository.allocation_for_request("container-finished") is None
    assert repository.allocation_for_request("container-running") is not None
    reservation = repository.get(finished.reservation_id)
    assert reservation is not None
    assert reservation.status is CapacityReservationStatus.Registered

    owners.active.clear()
    service.reconcile([worker], now=now + timedelta(seconds=3))

    assert repository.allocation_for_request("container-running") is None
    reservation = repository.get(finished.reservation_id)
    assert reservation is not None
    assert reservation.status is CapacityReservationStatus.Released


def test_unconfirmed_cancellation_cleanup_remains_open_and_blocks_owner_mutation(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller(release_status=CapacityAcquisitionStatus.TemporarilyUnavailable)
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    acquired = service.acquire(
        _request("container-cleanup-pending"), purchases=lambda: _purchases(service), now=now
    )

    service.release_request(
        "container-cleanup-pending",
        now=now + timedelta(seconds=1),
    )

    reservation = repository.get(acquired.reservation_id)
    assert reservation is not None
    assert reservation.open
    assert repository.allocations_for(reservation.id) == []
    assert service.has_open_reservations(OWNER_ID)


@pytest.mark.anyio
async def test_real_redis_dispatch_atomically_consumes_capacity_allocation(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    reservations = RedisCapacityReservationRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = _request("container-atomic")
    with reservations.mutation_lock(OWNER_ID):
        decision = reservations.reserve(
            capacity_owner_id=OWNER_ID,
            pool=MachinePool("default"),
            owner_kind=CapacityOwnerKind.PooledProvider,
            request=request,
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
    worker = _worker(OWNER_ID, created_at=now)
    workers.add_worker(worker, now=now)
    workers.toggle_worker_available(worker.worker_id, now=now)
    workers.enqueue_container_request(request, ready_at=now)
    claim = workers.claim_ready_container_requests(now=now, limit=1)[0]

    updated = workers.dispatch_claimed_container_request(
        worker.worker_id,
        claim,
        capacity_allocation=reservations.dispatch_allocation(request.container_id),
        now=now,
    )

    assert updated.free_cpu_millicores == worker.free_cpu_millicores - request.cpu_millicores
    assert reservations.allocation_for_request(request.container_id) is None
    assert reservations.allocations_for(decision.reservation.id) == []
    assert (
        await workers.wait_for_next_container_request(
            async_redis,
            worker.worker_id,
            timeout_seconds=0.01,
        )
        == request
    )


@pytest.mark.parametrize(
    ("first_updates", "second_updates"),
    [
        ({"cpu_millicores": 3_000}, {"cpu_millicores": 2_000}),
        ({"memory_mib": 2_000}, {"memory_mib": 2_000}),
        (
            {"gpu_type": "h100", "gpu_count": 2},
            {"gpu_type": "h100", "gpu_count": 1},
        ),
    ],
    ids=("cpu", "memory", "gpu"),
)
def test_cpu_memory_and_gpu_exhaustion_prevent_false_compatible_reuse(
    real_redis_actors: RealRedisActors,
    first_updates: dict[str, int | str],
    second_updates: dict[str, int | str],
) -> None:
    repository = _repository(real_redis_actors)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    is_gpu_case = "gpu_count" in first_updates
    shape = CapacityRequestShape(
        cpu_millicores=4_000,
        memory_mib=4_000,
        gpu_type="h100" if is_gpu_case else "",
        gpu_count=2 if is_gpu_case else 0,
        runtime_classes=("runsc",),
    )
    first_request = _request("dimension-a").model_copy(update=first_updates)
    second_request = _request("dimension-b").model_copy(update=second_updates)

    with repository.mutation_lock(OWNER_ID):
        first = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool=MachinePool("default"),
            owner_kind=CapacityOwnerKind.PooledProvider,
            request=first_request,
            shape=shape,
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
        second = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool=MachinePool("default"),
            owner_kind=CapacityOwnerKind.PooledProvider,
            request=second_request,
            shape=shape,
            registration_timeout=timedelta(minutes=10),
            now=now,
        )

    assert first.reservation.id != second.reservation.id


def test_concurrent_compatible_misses_deduplicate_after_lock_retry(
    real_redis_actors: RealRedisActors,
) -> None:
    controller = _Controller(delay_seconds=0.2)
    first_service = CapacityReservationService(_repository(real_redis_actors), lambda: [controller])
    second_service = CapacityReservationService(
        _repository(real_redis_actors), lambda: [controller]
    )
    barrier = Barrier(2)
    requests = (_request("concurrent-a"), _request("concurrent-b"))
    services = (first_service, second_service)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def acquire(index: int) -> CapacityAcquisitionResult | CapacityReservationLockContendedError:
        barrier.wait()
        try:
            return services[index].acquire(
                requests[index], purchases=lambda: _purchases(services[index]), now=now
            )
        except CapacityReservationLockContendedError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(acquire, range(2)))

    assert sum(isinstance(result, CapacityReservationLockContendedError) for result in results) == 1
    succeeded_index = next(
        index
        for index, result in enumerate(results)
        if isinstance(result, CapacityAcquisitionResult)
    )
    retry_index = 1 - succeeded_index
    retried = services[retry_index].acquire(
        requests[retry_index],
        purchases=lambda: _purchases(services[retry_index]),
        now=now + timedelta(seconds=1),
    )

    assert retried.status is CapacityAcquisitionStatus.ExistingPending
    [reservation] = first_service.reservations.list_for_owner(OWNER_ID)
    assert {
        allocation.container_id
        for allocation in first_service.reservations.allocations_for(reservation.id)
    } == {"concurrent-a", "concurrent-b"}


def test_reservation_repository_rejects_registered_state_regression(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with repository.mutation_lock(OWNER_ID):
        decision = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool=MachinePool("default"),
            owner_kind=CapacityOwnerKind.PooledProvider,
            request=_request("state-regression"),
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
        registered = repository.update(
            decision.reservation.model_copy(
                update={"status": CapacityReservationStatus.Registered}
            ),
            expected_resource_version=decision.reservation.resource_version,
            now=now,
        )
        with pytest.raises(CapacityReservationStateTransitionError):
            repository.update(
                registered.model_copy(update={"status": CapacityReservationStatus.Pending}),
                expected_resource_version=registered.resource_version,
                now=now,
            )


def test_capacity_owner_mutation_lock_renews_during_slow_owner_operation(
    real_redis_actors: RealRedisActors,
) -> None:
    owner = _repository(real_redis_actors)
    contender = _repository(real_redis_actors)

    # Contended from another thread, which is what a second holder is: the lease
    # re-enters for the caller that already holds it, so a contender sharing this
    # one's stack would be reporting on itself.
    with owner.mutation_lock(OWNER_ID, ttl_seconds=1):
        sleep(1.4)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(_enters_lease, contender).result() is False

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(_enters_lease, contender).result() is True


def test_capacity_owner_mutation_lock_re_enters_for_the_holder(
    real_redis_actors: RealRedisActors,
) -> None:
    """A decision under the lease calls services that take the same lease.

    The drain holds a capacity owner while it surges a replacement, and scaling
    the unit locks that owner for itself. Refusing the second acquisition is a
    deadlock against the caller's own lease, reported as another holder, and it
    stops a pool ever moving onto a new launch template.
    """

    owner = _repository(real_redis_actors)
    contender = _repository(real_redis_actors)

    with owner.mutation_lock(OWNER_ID):
        with owner.mutation_lock(OWNER_ID), ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(_enters_lease, contender).result() is False
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(_enters_lease, contender).result() is False

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(_enters_lease, contender).result() is True


def _enters_lease(repository: RedisCapacityReservationRepository) -> bool:
    try:
        with repository.mutation_lock(OWNER_ID):
            return True
    except CapacityReservationLockContendedError:
        return False


def _worker(capacity_owner_id: str, *, created_at: datetime) -> SchedulerWorkerRecord:
    return SchedulerWorkerRecord(
        runtime_image="container-worker:local",
        worker_id=f"worker-{capacity_owner_id[:4]}",
        pool=MachinePool("default"),
        capacity_owner_id=capacity_owner_id,
        machine_id="machine-1",
        status=SchedulerWorkerStatus.Available,
        runtime_class="runsc",
        runtime_classes=["runsc"],
        free_cpu_millicores=4_000,
        free_memory_mib=8_192,
        total_cpu_millicores=4_000,
        total_memory_mib=8_192,
        created_at=created_at,
        updated_at=created_at,
    )
