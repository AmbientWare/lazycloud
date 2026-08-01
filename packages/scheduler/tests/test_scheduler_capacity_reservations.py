from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Barrier
from time import sleep
from typing import Protocol

import pytest
from api.server.services import ApiServices
from compute.offers import ComputeOffer
from compute.projection import PoolConfig
from coordination.redis_client import RedisClient
from scheduler.capacity_reservations import (
    CapacityAcquisitionResult,
    CapacityAcquisitionStatus,
    CapacityProvisioningReservation,
    CapacityRequestShape,
    CapacityReservationLockContendedError,
    CapacityReservationService,
    CapacityReservationStateTransitionError,
    CapacityReservationStatus,
    ComputePoolCapacityController,
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
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
)
from shared.capacity import CapacityAcquisitionRequest as ComputeCapacityRequest
from shared.capacity import CapacityAcquisitionResult as ComputeCapacityResult
from shared.capacity import (
    CapacityOwnerKind,
    CapacityOwnerSource,
    CapacityPoolSizingState,
    CapacityPoolSizingStateUpdate,
    CapacityReleaseRequest,
)
from shared.compute_fleet import Pool
from shared.compute_policy import ComputePlacementSource
from shared.realtime.contracts import CloudEventRecord, EventDataInput, EventRecordType
from shared.scheduling import (
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    SchedulerWorkerStatus,
    WorkerUnavailableReason,
)
from tests.provider_fixtures import configure_test_provider

OWNER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_OWNER_ID = "22222222-2222-4222-8222-222222222222"


def _request(container_id: str, *, cpu: int = 1_000) -> SchedulerWorkerRequest:
    return SchedulerWorkerRequest(
        workspace_id="workspace-1",
        stub_id="stub-1",
        container_id=container_id,
        cpu_millicores=cpu,
        memory_mib=512,
        pool_selector="default",
        capacity_owner_id=OWNER_ID,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _shape() -> CapacityRequestShape:
    return CapacityRequestShape(
        cpu_millicores=4_000,
        memory_mib=8_192,
        runtime_classes=("runc",),
    )


class _RealRedisActors(Protocol):
    def client(self) -> RedisClient: ...


@dataclass(slots=True)
class _AllocationOwners:
    active: set[tuple[str, str]]

    def is_active(self, *, workspace_id: str, container_id: str) -> bool:
        return (workspace_id, container_id) in self.active


def _repository(real_redis_actors: _RealRedisActors) -> RedisCapacityReservationRepository:
    return RedisCapacityReservationRepository(real_redis_actors.client())


@dataclass(slots=True)
class _Controller:
    capacity_owner_id: str = OWNER_ID
    owner_kind: CapacityOwnerKind = CapacityOwnerKind.ManagedPool
    pool_name: str = "default"
    registration_timeout: timedelta = timedelta(minutes=10)
    ensure_calls: list[str] = field(default_factory=list)
    release_calls: list[str] = field(default_factory=list)
    ensure_status: CapacityAcquisitionStatus = CapacityAcquisitionStatus.Requested
    release_status: CapacityAcquisitionStatus = CapacityAcquisitionStatus.ExistingPending
    failures_remaining: int = 0
    delay_seconds: float = 0
    priority: int = 0
    health: CapacityPoolOperationalHealth = CapacityPoolOperationalHealth.Healthy
    target_machine_id: str = ""

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
            pool_name=self.pool_name,
            current_units=0,
            target_units=0,
            headroom=WorkerPoolEffectiveHeadroom(),
            initial_target_reached=True,
            reason="test controller has no proactive sizing",
        )

    def accepts(self, request: SchedulerWorkerRequest) -> bool:
        if request.capacity_owner_id:
            return (
                request.capacity_owner_id == self.capacity_owner_id
                and request.pool_selector in {"", self.pool_name}
            )
        if request.placement_source is ComputePlacementSource.AttachedPool:
            return request.pool_selector == self.pool_name
        return True

    def reservation_shape(self, request: SchedulerWorkerRequest) -> CapacityRequestShape:
        return reservation_shape_for_request(
            request,
            worker_cpu_millicores=4_000,
            worker_memory_mib=8_192,
            worker_gpu_type="",
            worker_gpu_count=0,
            worker_runtimes=("runc",),
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
            target_machine_id=self.target_machine_id,
        )


class _OwnerAgnosticController(_Controller):
    def accepts(self, request: SchedulerWorkerRequest) -> bool:
        _ = request
        return True


@dataclass(slots=True)
class _WorkerRepository:
    workers: list[SchedulerWorkerRecord] = field(default_factory=list)

    def list_workers(self) -> list[SchedulerWorkerRecord]:
        return list(self.workers)


@dataclass(slots=True)
class _SizingStates:
    pool: Pool
    revision: int = 0
    state: CapacityPoolSizingState | None = None

    def get_pool_sizing_state(self, capacity_owner_id: str) -> CapacityPoolSizingState:
        if capacity_owner_id != self.pool.capacity_owner_id:
            raise ValueError("unknown capacity owner")
        if self.state is None:
            self.state = CapacityPoolSizingState(
                capacity_owner_id=capacity_owner_id,
                pool_name=self.pool.name,
                workspace_id="workspace-1",
            )
        return self.state

    def compare_and_set_pool_sizing_state(
        self,
        update: CapacityPoolSizingStateUpdate,
    ) -> CapacityPoolSizingState:
        current = self.get_pool_sizing_state(update.capacity_owner_id)
        if current.revision != update.expected_revision:
            raise ValueError("sizing revision changed")
        self.state = CapacityPoolSizingState(
            capacity_owner_id=current.capacity_owner_id,
            pool_name=current.pool_name,
            workspace_id=current.workspace_id,
            revision=current.revision + 1,
            **update.model_dump(exclude={"capacity_owner_id", "expected_revision"}),
        )
        return self.state


@dataclass(slots=True)
class _UnusedComputeCapacity(_SizingStates):
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


def _managed_pool() -> Pool:
    return Pool(
        name="default",
        provider="generic",
        capacity_owner_id=OWNER_ID,
        capacity_owner_kind=CapacityOwnerKind.ManagedPool,
        capacity_owner_source=CapacityOwnerSource.Managed,
        max_workers=2,
        scaling_enabled=True,
        default_eligible=True,
        worker_cpu_millicores=4_000,
        worker_memory_mib=8_192,
    )


class _IdentityPlacement:
    def place(self, request: SchedulerWorkerRequest) -> SchedulerWorkerRequest:
        return request


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
    real_redis_actors: _RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    with repository.mutation_lock(OWNER_ID):
        first = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool_name="default",
            owner_kind=CapacityOwnerKind.ManagedPool,
            request=_request("container-1"),
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
        repeated = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool_name="default",
            owner_kind=CapacityOwnerKind.ManagedPool,
            request=_request("container-1"),
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
        second = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool_name="default",
            owner_kind=CapacityOwnerKind.ManagedPool,
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
    real_redis_actors: _RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with repository.mutation_lock(OWNER_ID):
        first = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool_name="shared-name",
            owner_kind=CapacityOwnerKind.ManagedPool,
            request=_request("container-1", cpu=3_000),
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
        second = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool_name="shared-name",
            owner_kind=CapacityOwnerKind.ManagedPool,
            request=_request("container-2", cpu=3_000),
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
    other_request = _request("container-3").model_copy(update={"capacity_owner_id": OTHER_OWNER_ID})
    with repository.mutation_lock(OTHER_OWNER_ID):
        other = repository.reserve(
            capacity_owner_id=OTHER_OWNER_ID,
            pool_name="shared-name",
            owner_kind=CapacityOwnerKind.ManagedPool,
            request=other_request,
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )

    assert first.reservation.id != second.reservation.id
    assert other.reservation.id not in {first.reservation.id, second.reservation.id}


def test_capacity_service_requests_one_unit_then_reuses_the_durable_intent(
    real_redis_actors: _RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller()
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)

    first = service.acquire(_request("container-1"), now=now)
    second = service.acquire(_request("container-2"), now=now)

    assert first.status is CapacityAcquisitionStatus.Requested
    assert second.status is CapacityAcquisitionStatus.ExistingPending
    assert second.reservation_id == first.reservation_id
    assert set(controller.ensure_calls) == {first.reservation_id}


def test_resolve_request_binds_and_fences_the_selected_capacity_owner() -> None:
    controller = _OwnerAgnosticController()
    service = CapacityReservationService(
        RedisCapacityReservationRepository(RedisClient.from_settings()),
        lambda: [controller],
    )
    unbound = _request("unbound").model_copy(
        update={
            "pool_selector": "",
            "capacity_owner_id": "",
            "placement_source": ComputePlacementSource.WorkspaceDefault,
        }
    )

    resolved = service.resolve_request(unbound)

    assert resolved.pool_selector == controller.pool_name
    assert resolved.capacity_owner_id == controller.capacity_owner_id
    assert service.resolve_request(resolved) == resolved
    owner_only = service.resolve_request(
        unbound.model_copy(update={"capacity_owner_id": controller.capacity_owner_id})
    )
    assert owner_only.pool_selector == controller.pool_name
    assert owner_only.capacity_owner_id == controller.capacity_owner_id
    with pytest.raises(ValueError, match="capacity owner does not match"):
        service.resolve_request(resolved.model_copy(update={"capacity_owner_id": OTHER_OWNER_ID}))


def test_terminal_retry_releases_stale_reservation_before_new_attempt(
    real_redis_actors: _RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller(ensure_status=CapacityAcquisitionStatus.Unsupported)
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)

    first = service.acquire(_request("container-terminal"), now=now)
    second = service.acquire(
        _request("container-terminal"),
        now=now + timedelta(seconds=1),
    )

    first_reservation = repository.get(first.reservation_id)
    assert first.status is CapacityAcquisitionStatus.Unsupported
    assert second.status is CapacityAcquisitionStatus.Unsupported
    assert first.reservation_id != second.reservation_id
    assert first_reservation is not None
    assert first_reservation.status is CapacityReservationStatus.Released


def test_a_full_pool_keeps_one_open_claim_instead_of_churning_released_ones(
    real_redis_actors: _RealRedisActors,
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

    first = service.acquire(_request("container-at-limit"), now=now)
    second = service.acquire(
        _request("container-at-limit"),
        now=now + timedelta(seconds=1),
    )

    assert first.status is CapacityAcquisitionStatus.AtLimit
    assert second.status is CapacityAcquisitionStatus.AtLimit
    assert first.reservation_id == second.reservation_id
    retained = repository.get(first.reservation_id)
    assert retained is not None
    assert retained.open


def test_unpinned_acquisition_fails_over_from_at_limit_pool_in_priority_order(
    real_redis_actors: _RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    primary = _Controller(
        ensure_status=CapacityAcquisitionStatus.AtLimit,
        priority=20,
    )
    fallback = _Controller(
        capacity_owner_id=OTHER_OWNER_ID,
        pool_name="fallback",
        priority=10,
    )
    service = CapacityReservationService(repository, lambda: [fallback, primary])
    request = _request("failover").model_copy(
        update={
            "capacity_owner_id": "",
            "placement_source": ComputePlacementSource.WorkspaceDefault,
        }
    )

    result = service.acquire(request, now=datetime(2026, 1, 1, tzinfo=UTC))

    assert result.status is CapacityAcquisitionStatus.Requested
    assert result.capacity_owner_id == OTHER_OWNER_ID
    assert len(primary.ensure_calls) == 1
    assert len(fallback.ensure_calls) == 1
    primary_reservation = repository.get(primary.ensure_calls[0])
    assert primary_reservation is not None
    assert primary_reservation.status is CapacityReservationStatus.Released


def test_attached_pool_at_limit_remains_strict_without_fallback(
    real_redis_actors: _RealRedisActors,
) -> None:
    primary = _Controller(ensure_status=CapacityAcquisitionStatus.AtLimit, priority=1)
    fallback = _Controller(
        capacity_owner_id=OTHER_OWNER_ID,
        pool_name="fallback",
        priority=100,
    )
    service = CapacityReservationService(
        _repository(real_redis_actors),
        lambda: [fallback, primary],
    )
    request = _request("strict").model_copy(
        update={
            "capacity_owner_id": "",
            "placement_source": ComputePlacementSource.AttachedPool,
        }
    )

    result = service.acquire(request, now=datetime(2026, 1, 1, tzinfo=UTC))

    assert result.status is CapacityAcquisitionStatus.AtLimit
    assert result.capacity_owner_id == OWNER_ID
    assert fallback.ensure_calls == []


def test_fixed_pool_rejects_cross_workspace_and_oversized_capacity_requests() -> None:
    compute = ComputePoolCapacityController(
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


def test_placement_miss_transfers_capacity_to_dispatch_before_reconciliation(
    real_redis_actors: _RealRedisActors,
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


def test_final_dispatch_rechecks_owner_worker_after_scale_zero_mutation(
    real_redis_actors: _RealRedisActors,
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
        with capacity.mutation_lock(OWNER_ID):
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
    assert workers.get_next_container_request(worker.worker_id) is None
    assert workers.has_recoverable_container_request(request.container_id)
    state = containers.get_container_state(request.container_id)
    assert state is not None
    assert state.worker_id == ""


def test_managed_provider_miss_persists_exact_unit_and_releases_only_owned_machine(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
) -> None:
    provider = configure_test_provider(
        isolated_services,
        "generic",
        [
            ComputeOffer(
                id="capacity-medium",
                provider="generic",
                instance_type="capacity-medium",
                region="lab",
                cpu_millicores=4_000,
                memory_mb=8_192,
                hourly_cost_micros=250_000,
                available=3,
            )
        ],
    )
    isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="managed-capacity",
            providers=["generic"],
            nodes=1,
            ttl="1h",
            max_spend=10.0,
        )
    )
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    pool = next(
        item
        for item in isolated_services.compute.list_pools(workspace=workspace_id)
        if item.name == "managed-capacity"
    ).model_copy(
        update={
            "capacity_owner_kind": CapacityOwnerKind.ManagedPool,
            "capacity_owner_source": CapacityOwnerSource.Managed,
            "scaling_enabled": True,
            "worker_cpu_millicores": 4_000,
            "worker_memory_mib": 8_192,
        }
    )
    service = CapacityReservationService(
        _repository(real_redis_actors),
        lambda: [
            ComputePoolCapacityController(
                workspace_id,
                pool,
                isolated_services.compute,
                _WorkerRepository(),
            )
        ],
    )
    request = _request("managed-placement-miss").model_copy(
        update={
            "workspace_id": workspace_id,
            "pool_selector": pool.name,
            "capacity_owner_id": pool.capacity_owner_id,
        }
    )

    acquired = service.acquire(request)
    repeated = service.acquire(request)
    reservation = service.reservations.get(acquired.reservation_id)

    assert acquired.status is CapacityAcquisitionStatus.Requested
    assert repeated.status is CapacityAcquisitionStatus.ExistingPending
    assert reservation is not None
    assert reservation.desired_unit == 2
    assert len(provider.list_machines(pool.name)) == 2

    service.release_request(request.container_id)

    released = service.reservations.get(acquired.reservation_id)
    assert released is not None
    assert released.status is CapacityReservationStatus.Released
    assert len(provider.list_machines(pool.name)) == 1


def test_available_worker_registration_uses_reported_schedulable_capacity(
    real_redis_actors: _RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller(target_machine_id="machine-1")
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    acquired = service.acquire(_request("container-1"), now=now)
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
    real_redis_actors: _RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller(registration_timeout=timedelta(seconds=30))
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    acquired = service.acquire(_request("container-1"), now=now)

    reconciled = service.reconcile([], now=now + timedelta(seconds=31))

    assert controller.release_calls == [acquired.reservation_id]
    assert reconciled[-1].status is CapacityReservationStatus.Expired


def test_cancellation_releases_exact_owned_capacity_after_last_allocation(
    real_redis_actors: _RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller()
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    acquired = service.acquire(_request("container-cancelled"), now=now)

    service.release_request(
        "container-cancelled",
        now=now + timedelta(seconds=1),
    )

    reservation = repository.get(acquired.reservation_id)
    assert controller.release_calls == [acquired.reservation_id]
    assert reservation is not None
    assert reservation.status is CapacityReservationStatus.Released


def test_cancellation_after_registration_keeps_capacity_for_idle_drain(
    real_redis_actors: _RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller()
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    acquired = service.acquire(_request("container-registered-cancel"), now=now)
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
    real_redis_actors: _RealRedisActors,
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
    finished = service.acquire(_request("container-finished"), now=now)
    running = service.acquire(_request("container-running"), now=now)
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
    real_redis_actors: _RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    controller = _Controller(release_status=CapacityAcquisitionStatus.TemporarilyUnavailable)
    service = CapacityReservationService(repository, lambda: [controller])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    acquired = service.acquire(_request("container-cleanup-pending"), now=now)

    service.release_request(
        "container-cleanup-pending",
        now=now + timedelta(seconds=1),
    )

    reservation = repository.get(acquired.reservation_id)
    assert reservation is not None
    assert reservation.open
    assert repository.allocations_for(reservation.id) == []
    assert service.has_open_reservations(OWNER_ID)


def test_real_redis_dispatch_atomically_consumes_capacity_allocation(
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    reservations = RedisCapacityReservationRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = _request("container-atomic")
    with reservations.mutation_lock(OWNER_ID):
        decision = reservations.reserve(
            capacity_owner_id=OWNER_ID,
            pool_name="default",
            owner_kind=CapacityOwnerKind.ManagedPool,
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
    assert workers.get_next_container_request(worker.worker_id) == request


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
    real_redis_actors: _RealRedisActors,
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
        runtime_classes=("runc",),
    )
    first_request = _request("dimension-a").model_copy(update=first_updates)
    second_request = _request("dimension-b").model_copy(update=second_updates)

    with repository.mutation_lock(OWNER_ID):
        first = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool_name="default",
            owner_kind=CapacityOwnerKind.ManagedPool,
            request=first_request,
            shape=shape,
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
        second = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool_name="default",
            owner_kind=CapacityOwnerKind.ManagedPool,
            request=second_request,
            shape=shape,
            registration_timeout=timedelta(minutes=10),
            now=now,
        )

    assert first.reservation.id != second.reservation.id


def test_concurrent_compatible_misses_deduplicate_after_lock_retry(
    real_redis_actors: _RealRedisActors,
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
            return services[index].acquire(requests[index], now=now)
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
        now=now + timedelta(seconds=1),
    )

    assert retried.status is CapacityAcquisitionStatus.ExistingPending
    [reservation] = first_service.reservations.list_for_owner(OWNER_ID)
    assert {
        allocation.container_id
        for allocation in first_service.reservations.allocations_for(reservation.id)
    } == {"concurrent-a", "concurrent-b"}


def test_reservation_repository_rejects_registered_state_regression(
    real_redis_actors: _RealRedisActors,
) -> None:
    repository = _repository(real_redis_actors)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with repository.mutation_lock(OWNER_ID):
        decision = repository.reserve(
            capacity_owner_id=OWNER_ID,
            pool_name="default",
            owner_kind=CapacityOwnerKind.ManagedPool,
            request=_request("state-regression"),
            shape=_shape(),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
        provisioning = repository.update(
            decision.reservation.model_copy(
                update={"status": CapacityReservationStatus.Provisioning}
            ),
            expected_resource_version=decision.reservation.resource_version,
            now=now,
        )
        registered = repository.update(
            provisioning.model_copy(update={"status": CapacityReservationStatus.Registered}),
            expected_resource_version=provisioning.resource_version,
            now=now,
        )
        with pytest.raises(CapacityReservationStateTransitionError):
            repository.update(
                registered.model_copy(update={"status": CapacityReservationStatus.Provisioning}),
                expected_resource_version=registered.resource_version,
                now=now,
            )


def test_capacity_owner_mutation_lock_renews_during_slow_owner_operation(
    real_redis_actors: _RealRedisActors,
) -> None:
    owner = _repository(real_redis_actors)
    contender = _repository(real_redis_actors)

    with owner.mutation_lock(OWNER_ID, ttl_seconds=1):
        sleep(1.4)
        with (
            pytest.raises(CapacityReservationLockContendedError),
            contender.mutation_lock(OWNER_ID, ttl_seconds=1),
        ):
            raise AssertionError("contender must not enter a renewed owner lease")

    with contender.mutation_lock(OWNER_ID, ttl_seconds=1):
        pass


def _worker(capacity_owner_id: str, *, created_at: datetime) -> SchedulerWorkerRecord:
    return SchedulerWorkerRecord(
        worker_id=f"worker-{capacity_owner_id[:4]}",
        pool_name="default",
        capacity_owner_id=capacity_owner_id,
        machine_id="machine-1",
        status=SchedulerWorkerStatus.Available,
        runtime_class="runc",
        runtime_classes=["runc"],
        free_cpu_millicores=4_000,
        free_memory_mib=8_192,
        total_cpu_millicores=4_000,
        total_memory_mib=8_192,
        created_at=created_at,
        updated_at=created_at,
    )
