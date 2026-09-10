from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from compute.agent_control import agent_machine_worker_id
from compute.capacity_errors import CapacityReservationLockContendedError
from compute.service import ComputeService
from compute.state import RedisComputeStateRepository
from coordination.redis_client import RedisClient
from database.repositories.compute import ComputeMachineEnrollmentRepository, ComputeUnitRepository
from database.repositories.orchestration import ContainerRepository, WorkerRepository
from gateway.http import JoinAgentRequest
from gateway.service import GatewayControlService
from gateway.unit_state import billing_owner_for_unit
from scheduler.fleet import WorkerPoolStateSnapshot
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerPoolStateRepository,
    WorkerPoolStateNotFoundError,
)
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_enrollment import (
    AgentWorkerSlotStatus,
    ComputePreflightCheck,
    PreflightSeverity,
)
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitPhase,
    ComputeUnitProviderState,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    MachinePool,
    UnitName,
)
from shared.containers import ContainerRecord
from shared.errors import ConflictError, InvalidInputError
from shared.scheduling import (
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
    SchedulerWorkerStatus,
)
from shared.usage import UsageBillingOwner
from tests.domain_fixtures import workspace_owner_user_id
from tests.redis_fakes import FakeRedis


class _RecordingCapacityReservationGuard:
    def __init__(self, *, open_reservations: bool) -> None:
        self.open_reservations = open_reservations
        self.active_capacity_owner_id = ""
        self.active_dispatch_owner_id = ""
        self.locked_capacity_owner_ids: list[str] = []
        self.dispatch_locked_capacity_owner_ids: list[str] = []
        self.checked_capacity_owner_ids: list[str] = []
        self.events: list[str] = []

    @contextmanager
    def mutation_lock(self, capacity_owner_id: str) -> Iterator[None]:
        assert not self.active_capacity_owner_id
        self.active_capacity_owner_id = capacity_owner_id
        self.locked_capacity_owner_ids.append(capacity_owner_id)
        self.events.append("lock-enter")
        try:
            yield
        finally:
            self.events.append("lock-exit")
            self.active_capacity_owner_id = ""

    @contextmanager
    def dispatch_lock(self, capacity_owner_id: str) -> Iterator[None]:
        assert not self.active_dispatch_owner_id
        self.active_dispatch_owner_id = capacity_owner_id
        self.dispatch_locked_capacity_owner_ids.append(capacity_owner_id)
        self.events.append("dispatch-lock-enter")
        try:
            yield
        finally:
            self.events.append("dispatch-lock-exit")
            self.active_dispatch_owner_id = ""

    def has_open_reservations(self, capacity_owner_id: str) -> bool:
        assert self.active_capacity_owner_id == capacity_owner_id
        self.checked_capacity_owner_ids.append(capacity_owner_id)
        self.events.append("reservation-check")
        return self.open_reservations


def _gateway(
    services: ApiServices,
    guard: _RecordingCapacityReservationGuard,
    *,
    key_prefix: str,
) -> GatewayControlService:
    redis = RedisClient(FakeRedis(), key_prefix=key_prefix)
    return replace(
        services.gateway_service,
        compute_state=RedisComputeStateRepository(redis),
        scheduler_workers=RedisSchedulerWorkerRepository(redis),
        scheduler_containers=RedisSchedulerContainerRepository(redis),
        scheduler_pool_states=RedisWorkerPoolStateRepository(redis),
        capacity_reservations=guard,
    )


def _default_workspace_id(services: ApiServices) -> str:
    with services.context.database.session() as session:
        return services.context.default_workspace_id(session)


def _own_default_workspace(services: ApiServices) -> str:
    """The account a joined machine belongs to: the one that owns its workspace."""
    return workspace_owner_user_id(services.context, _default_workspace_id(services))


def _join_request(join_token: str) -> JoinAgentRequest:
    return JoinAgentRequest(
        join_token=join_token,
        machine_fingerprint="guarded-machine-fingerprint",
        hostname="guarded-machine",
        os="linux",
        arch="amd64",
        cpu_count=4,
        cpu_millicores=4_000,
        memory_mb=8_192,
        preflight=[
            ComputePreflightCheck(
                name="container-runtime",
                ok=True,
                severity=PreflightSeverity.Error,
                remediation="Install and start the container runtime.",
            )
        ],
    )


def _scalable_pool(
    services: ApiServices,
    *,
    workspace_id: str,
    pool: MachinePool,
    capacity_owner_id: str,
) -> ComputeUnitRecord:
    services.compute.create_unit(
        UnitName(pool),
        workspace=workspace_id,
        provider="aws:test-connection",
        capacity_owner_id=capacity_owner_id,
        initial_machines=1,
        min_machines=0,
        max_machines=2,
        scaling_enabled=True,
        worker_cpu_millicores=4_000,
        worker_memory_mib=8_192,
    )
    return ComputeUnitRecord(
        id=capacity_owner_id,
        workspace_id=workspace_id,
        capacity_owner_id=capacity_owner_id,
        capacity_owner_kind=CapacityOwnerKind.PooledProvider,
        capacity_owner_source=CapacityOwnerSource.Provider,
        name=UnitName(pool),
        pool=MachinePool(pool),
        provider_ref="aws:test-connection",
        provider_connection_id="11111111-1111-4111-8111-111111111111",
        capacity_mode=ComputeCapacityMode.Pooled,
        visibility=ComputeUnitVisibility.Internal,
        region="us-east-1",
        offer_id="us-east-1:test.instance",
        capability_key="aws:us-east-1:test.instance:amd64:runsc",
        desired_machines=1,
        min_machines=0,
        max_machines=2,
        observed_machines=1,
        phase=ComputeUnitPhase.Ready,
        provider_state=ComputeUnitProviderState(resource_id="test-pool"),
    )


def _install_recording_scale(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current: ComputeUnitRecord,
    guard: _RecordingCapacityReservationGuard,
    mutation_calls: list[tuple[str, str, int]],
) -> None:
    def scale_internal_unit(
        _compute: ComputeService,
        workspace_id: str,
        capacity_owner_id: str,
        desired_machines: int,
        *,
        before_mutation: Callable[[ComputeUnitRecord], None],
        now: datetime | None = None,
    ) -> ComputeUnitRecord:
        del now
        with guard.mutation_lock(current.capacity_owner_id):
            guard.events.append("intent-read")
            assert (workspace_id, capacity_owner_id) == (
                current.workspace_id,
                current.capacity_owner_id,
            )
            before_mutation(current)
            assert guard.active_capacity_owner_id == current.capacity_owner_id
            guard.events.append("compute-scale")
            mutation_calls.append((workspace_id, capacity_owner_id, desired_machines))
        return current.model_copy(
            update={
                "desired_machines": desired_machines,
                "observed_machines": desired_machines,
            }
        )

    monkeypatch.setattr(ComputeService, "scale_internal_unit", scale_internal_unit)


def test_pool_scale_delegates_to_compute_while_capacity_owner_lock_is_held(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    pool = "stateful-scale-pool"
    capacity_owner_id = "11111111-2222-4333-8444-555555555555"
    current = _scalable_pool(
        isolated_services,
        workspace_id=workspace_id,
        pool=MachinePool(pool),
        capacity_owner_id=capacity_owner_id,
    )
    guard = _RecordingCapacityReservationGuard(open_reservations=False)
    gateway = _gateway(isolated_services, guard, key_prefix="pool-stateful-scale")
    mutation_calls: list[tuple[str, str, int]] = []
    _install_recording_scale(
        monkeypatch,
        current=current,
        guard=guard,
        mutation_calls=mutation_calls,
    )

    scaled = gateway.scale_unit(current.id, 0, workspace_id=workspace_id)

    assert scaled.desired_machines == 0
    assert mutation_calls == [(workspace_id, capacity_owner_id, 0)]
    assert guard.locked_capacity_owner_ids == [capacity_owner_id]
    assert guard.checked_capacity_owner_ids == [capacity_owner_id]
    assert guard.events == [
        "lock-enter",
        "intent-read",
        "reservation-check",
        "compute-scale",
        "lock-exit",
    ]


def test_pool_scale_refuses_open_reservation_before_compute_mutation(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    pool = "reserved-scale-pool"
    capacity_owner_id = "22222222-3333-4444-8555-666666666666"
    current = _scalable_pool(
        isolated_services,
        workspace_id=workspace_id,
        pool=MachinePool(pool),
        capacity_owner_id=capacity_owner_id,
    )
    guard = _RecordingCapacityReservationGuard(open_reservations=True)
    gateway = _gateway(isolated_services, guard, key_prefix="pool-reserved-scale")
    mutation_calls: list[tuple[str, str, int]] = []
    _install_recording_scale(
        monkeypatch,
        current=current,
        guard=guard,
        mutation_calls=mutation_calls,
    )

    with pytest.raises(ConflictError, match="active capacity reservations"):
        gateway.scale_unit(current.id, 0, workspace_id=workspace_id)

    assert mutation_calls == []
    assert guard.events == [
        "lock-enter",
        "intent-read",
        "reservation-check",
        "lock-exit",
    ]


def test_pool_scale_zero_refuses_active_reservation_when_stored_capacity_is_zero(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    pool = "zero-state-repair-pool"
    capacity_owner_id = "24444444-3555-4666-8777-888888888888"
    current = _scalable_pool(
        isolated_services,
        workspace_id=workspace_id,
        pool=MachinePool(pool),
        capacity_owner_id=capacity_owner_id,
    ).model_copy(update={"desired_machines": 0, "observed_machines": 0})
    guard = _RecordingCapacityReservationGuard(open_reservations=True)
    gateway = _gateway(isolated_services, guard, key_prefix="pool-zero-state-repair")
    mutation_calls: list[tuple[str, str, int]] = []
    _install_recording_scale(
        monkeypatch,
        current=current,
        guard=guard,
        mutation_calls=mutation_calls,
    )

    with pytest.raises(ConflictError, match="active capacity reservations"):
        gateway.scale_unit(current.id, 0, workspace_id=workspace_id)

    assert mutation_calls == []
    assert guard.events == [
        "lock-enter",
        "intent-read",
        "reservation-check",
        "lock-exit",
    ]


def test_pool_scale_zero_refuses_unassigned_pending_workspace_container(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    pool = "unassigned-pending-pool"
    capacity_owner_id = "25444444-3555-4666-8777-888888888888"
    current = _scalable_pool(
        isolated_services,
        workspace_id=workspace_id,
        pool=MachinePool(pool),
        capacity_owner_id=capacity_owner_id,
    )
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="pending-before-pool-resolution",
                image="",
                command=[],
                workspace_id=workspace_id,
            )
        )
    guard = _RecordingCapacityReservationGuard(open_reservations=False)
    gateway = _gateway(isolated_services, guard, key_prefix="pool-unassigned-pending")
    mutation_calls: list[tuple[str, str, int]] = []
    _install_recording_scale(
        monkeypatch,
        current=current,
        guard=guard,
        mutation_calls=mutation_calls,
    )

    with pytest.raises(ConflictError, match="pending or running containers"):
        gateway.scale_unit(current.id, 0, workspace_id=workspace_id)

    assert mutation_calls == []


def test_pool_scale_zero_disables_owner_worker_before_compute_mutation(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    pool = "dispatch-fenced-zero-pool"
    capacity_owner_id = "26444444-3555-4666-8777-888888888888"
    current = _scalable_pool(
        isolated_services,
        workspace_id=workspace_id,
        pool=MachinePool(pool),
        capacity_owner_id=capacity_owner_id,
    )
    guard = _RecordingCapacityReservationGuard(open_reservations=False)
    gateway = _gateway(isolated_services, guard, key_prefix="pool-dispatch-fence")
    worker = SchedulerWorkerRecord(
        worker_id="idle-provider-worker",
        pool=MachinePool(pool),
        capacity_owner_id=capacity_owner_id,
        status=SchedulerWorkerStatus.Available,
    )
    disabled: list[str] = []

    def list_workers(
        _repository: RedisSchedulerWorkerRepository,
    ) -> list[SchedulerWorkerRecord]:
        return [worker]

    def disable_worker(
        _repository: RedisSchedulerWorkerRepository,
        worker_id: str,
        *,
        reason: str,
    ) -> SchedulerWorkerRecord:
        _ = reason
        disabled.append(worker_id)
        guard.events.append("worker-disabled")
        return worker.model_copy(update={"status": SchedulerWorkerStatus.Unavailable})

    monkeypatch.setattr(RedisSchedulerWorkerRepository, "list_workers", list_workers)
    monkeypatch.setattr(RedisSchedulerWorkerRepository, "disable_worker", disable_worker)
    mutation_calls: list[tuple[str, str, int]] = []
    _install_recording_scale(
        monkeypatch,
        current=current,
        guard=guard,
        mutation_calls=mutation_calls,
    )

    gateway.scale_unit(current.id, 0, workspace_id=workspace_id)

    assert disabled == [worker.worker_id]
    assert mutation_calls == [(workspace_id, capacity_owner_id, 0)]
    assert guard.events.index("worker-disabled") < guard.events.index("compute-scale")


def test_pool_state_refuses_mismatched_durable_capacity_owner(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    pool = "isolated-state-pool"
    current = _scalable_pool(
        isolated_services,
        workspace_id=workspace_id,
        pool=MachinePool(pool),
        capacity_owner_id="25555555-3666-4777-8888-999999999999",
    )

    def get_internal_unit(
        _compute: ComputeService,
        requested_workspace_id: str,
        requested_owner_id: str,
    ) -> ComputeUnitRecord:
        assert (requested_workspace_id, requested_owner_id) == (
            workspace_id,
            current.capacity_owner_id,
        )
        return current.model_copy(
            update={
                "id": "26666666-3777-4888-8999-000000000000",
                "capacity_owner_id": "26666666-3777-4888-8999-000000000000",
            }
        )

    monkeypatch.setattr(ComputeService, "get_internal_unit", get_internal_unit)
    guard = _RecordingCapacityReservationGuard(open_reservations=False)
    gateway = _gateway(isolated_services, guard, key_prefix="pool-state-owner-isolation")

    with pytest.raises(ConflictError, match="capacity ownership does not match"):
        gateway.unit_state(current.id, workspace_id=workspace_id)


@pytest.mark.parametrize(
    "container_status",
    [SchedulerContainerStatus.Pending, SchedulerContainerStatus.Running],
)
def test_pool_scale_refuses_active_pool_container_before_compute_mutation(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
    container_status: SchedulerContainerStatus,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    pool = f"active-{container_status.value}-pool"
    capacity_owner_id = "33333333-4444-4555-8666-777777777777"
    current = _scalable_pool(
        isolated_services,
        workspace_id=workspace_id,
        pool=MachinePool(pool),
        capacity_owner_id=capacity_owner_id,
    )
    guard = _RecordingCapacityReservationGuard(open_reservations=False)
    gateway = _gateway(
        isolated_services,
        guard,
        key_prefix=f"pool-active-{container_status.value}-scale",
    )
    worker_id = f"worker-{container_status.value}"
    worker = SchedulerWorkerRecord(
        worker_id=worker_id,
        pool=MachinePool(pool),
        capacity_owner_id=capacity_owner_id,
        status=SchedulerWorkerStatus.Available,
    )
    container = SchedulerContainerState(
        container_id=f"container-{container_status.value}",
        stub_id="stub-active-scale",
        workspace_id=workspace_id,
        worker_id=worker_id,
        status=container_status,
    )

    def list_workers(
        _repository: RedisSchedulerWorkerRepository,
    ) -> list[SchedulerWorkerRecord]:
        return [worker]

    def list_by_worker(
        _repository: RedisSchedulerContainerRepository,
        owner_worker_id: str,
    ) -> list[SchedulerContainerState]:
        return [container] if owner_worker_id == worker_id else []

    monkeypatch.setattr(
        RedisSchedulerWorkerRepository,
        "list_workers",
        list_workers,
    )
    monkeypatch.setattr(
        RedisSchedulerContainerRepository,
        "list_by_worker",
        list_by_worker,
    )
    mutation_calls: list[tuple[str, str, int]] = []
    _install_recording_scale(
        monkeypatch,
        current=current,
        guard=guard,
        mutation_calls=mutation_calls,
    )

    with pytest.raises(ConflictError, match="pending or running containers"):
        gateway.scale_unit(current.id, 0, workspace_id=workspace_id)

    assert mutation_calls == []
    assert guard.events == [
        "lock-enter",
        "intent-read",
        "reservation-check",
        "lock-exit",
    ]


def test_pool_delete_refuses_open_capacity_reservation_without_mutating_owned_state(
    isolated_services: ApiServices,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    _own_default_workspace(isolated_services)
    unit_name = "reservation-guarded-pool"
    capacity_owner_id = "dfd9f90a-f4af-41ee-8873-991a9fa860fe"
    isolated_services.compute.create_unit(
        UnitName(unit_name),
        workspace=workspace_id,
        provider="agent",
        capacity_owner_id=capacity_owner_id,
        max_machines=2,
        scaling_enabled=True,
        worker_cpu_millicores=4_000,
        worker_memory_mib=8_192,
    )
    guard = _RecordingCapacityReservationGuard(open_reservations=True)
    gateway = _gateway(isolated_services, guard, key_prefix="pool-reservation-guard")
    join = gateway.unit_state_coordinator.create_unit_join_token(
        gateway.unit_state_coordinator.unit_by_name(UnitName(unit_name), workspace_id=workspace_id),
        workspace_id=workspace_id,
        owner_token_id="gateway-test-owner",
    )
    enrolled = gateway.join_agent(_join_request(join.token))
    worker_id = agent_machine_worker_id(enrolled.machine_id)
    scheduler_pool_state = WorkerPoolStateSnapshot(
        capacity_owner_id=capacity_owner_id,
        pool=MachinePool(unit_name),
        available_workers=1,
        registered_machines=1,
    )
    gateway.scheduler_pool_state_repository.set_state(
        capacity_owner_id,
        scheduler_pool_state,
    )

    with pytest.raises(ConflictError, match="active capacity reservations"):
        gateway.delete_unit(capacity_owner_id, workspace_id=workspace_id)

    assert guard.locked_capacity_owner_ids == [capacity_owner_id]
    assert guard.dispatch_locked_capacity_owner_ids == [capacity_owner_id]
    assert guard.checked_capacity_owner_ids == [capacity_owner_id]
    assert guard.events == [
        "lock-enter",
        "dispatch-lock-enter",
        "reservation-check",
        "dispatch-lock-exit",
        "lock-exit",
    ]
    assert gateway.scheduler_pool_state_repository.get_state(capacity_owner_id) == (
        scheduler_pool_state
    )
    assert gateway.compute_states.get_unit_state(workspace_id, capacity_owner_id) is not None
    assert [
        item.name
        for item in isolated_services.compute.list_units(workspace=workspace_id)
        if item.name == unit_name
    ] == [unit_name]
    with isolated_services.context.database.session() as session:
        assert (
            len(
                ComputeMachineEnrollmentRepository(session).list_for_unit(
                    workspace_id,
                    capacity_owner_id,
                )
            )
            == 1
        )
        assert WorkerRepository(session).get_across_workspaces(worker_id) is not None


def test_pool_delete_uses_durable_capacity_owner_for_guard_and_scheduler_state(
    isolated_services: ApiServices,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    unit_name = "display-name-is-not-owner"
    capacity_owner_id = "71ee746b-674e-4125-a12a-21c3350abf83"
    isolated_services.compute.create_unit(
        UnitName(unit_name),
        workspace=workspace_id,
        provider="agent",
        capacity_owner_id=capacity_owner_id,
        max_machines=1,
    )
    guard = _RecordingCapacityReservationGuard(open_reservations=False)
    gateway = _gateway(isolated_services, guard, key_prefix="pool-durable-owner-delete")
    gateway.unit_state_coordinator.ensure_compute_pool_state(
        gateway.unit_state_coordinator.unit_by_name(UnitName(unit_name), workspace_id=workspace_id),
        workspace_id=workspace_id,
    )
    gateway.scheduler_pool_state_repository.set_state(
        capacity_owner_id,
        WorkerPoolStateSnapshot(
            capacity_owner_id=capacity_owner_id,
            pool=MachinePool(unit_name),
        ),
    )

    gateway.delete_unit(capacity_owner_id, workspace_id=workspace_id)

    assert guard.locked_capacity_owner_ids == [capacity_owner_id]
    assert guard.dispatch_locked_capacity_owner_ids == [capacity_owner_id]
    assert guard.checked_capacity_owner_ids == [capacity_owner_id]
    assert guard.events == [
        "lock-enter",
        "dispatch-lock-enter",
        "reservation-check",
        "dispatch-lock-exit",
        "lock-exit",
    ]
    assert all(
        item.name != unit_name
        for item in isolated_services.compute.list_units(workspace=workspace_id)
    )
    assert gateway.compute_states.get_unit_state(workspace_id, capacity_owner_id) is None
    with pytest.raises(WorkerPoolStateNotFoundError):
        gateway.scheduler_pool_state_repository.get_state(capacity_owner_id)


def test_join_credentials_are_refused_for_provider_provisioned_units(
    isolated_services: ApiServices,
) -> None:
    """A machine can only be joined to a unit machines are brought to.

    An internal unit is provisioned into a connected cloud account and its
    machines arrive through provider enrollment. Minting a join credential
    against one would let a machine somebody owns enroll under a provider-backed
    capacity owner, and be accounted for as that account's capacity — which is
    the field that decides whether a management fee applies.
    """

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    joinable = isolated_services.compute.create_unit(
        UnitName("joinable-unit"),
        workspace=workspace_id,
        provider="agent",
        capacity_owner_id=str(uuid4()),
        max_machines=1,
    )
    guard = _RecordingCapacityReservationGuard(open_reservations=False)
    gateway = _gateway(isolated_services, guard, key_prefix="join")

    # The unit machines are brought to mints a credential.
    assert gateway.create_unit_join_token(
        joinable.id, workspace_id=workspace_id, owner_token_id=""
    ).token

    internal = joinable.model_copy(update={"visibility": ComputeUnitVisibility.Internal})
    with pytest.raises(InvalidInputError, match="cannot be joined"):
        gateway.unit_state_coordinator.create_unit_join_token(
            internal,
            workspace_id=workspace_id,
            owner_token_id="",
        )


@pytest.mark.parametrize("surge", [False, True])
def test_rollout_settlement_defers_contended_maintenance(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
    surge: bool,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    unit = isolated_services.compute.create_unit(
        UnitName("rollout-settlement"), workspace=workspace_id, provider="agent"
    )
    with isolated_services.context.database.session() as session:
        repository = ComputeUnitRepository(session)
        repository.set_worker_rollout_surge(
            unit.id, expected_generation=unit.generation, enabled=surge
        )
    guard = _RecordingCapacityReservationGuard(open_reservations=False)
    gateway = _gateway(isolated_services, guard, key_prefix="rollout-settlement")

    def contended(
        self: _RecordingCapacityReservationGuard, capacity_owner_id: str
    ) -> AbstractContextManager[None]:
        del self, capacity_owner_id
        if not surge:
            raise RuntimeError("a settled rollout must not contend with capacity mutations")
        raise CapacityReservationLockContendedError("another reconciler owns capacity")

    monkeypatch.setattr(_RecordingCapacityReservationGuard, "mutation_lock", contended)
    gateway._settle_worker_rollout_capacity(
        SchedulerWorkerRecord(
            worker_id="rollout-worker", capacity_owner_id=unit.capacity_owner_id, pool=unit.pool
        ),
        target_image="worker:target",
    )
    with isolated_services.context.database.session() as session:
        current = ComputeUnitRepository(session).get(unit.id)
    assert current is not None and current.worker_rollout_surge is surge


@pytest.mark.parametrize(
    "worker_status,slot_status",
    [
        (SchedulerWorkerStatus.Available, AgentWorkerSlotStatus.Active),
        (SchedulerWorkerStatus.Draining, AgentWorkerSlotStatus.Draining),
    ],
)
def test_rollout_contention_preserves_worker_without_restart_authorization(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
    worker_status: SchedulerWorkerStatus,
    slot_status: AgentWorkerSlotStatus,
) -> None:
    workspace_id = _default_workspace_id(isolated_services)
    _own_default_workspace(isolated_services)
    unit = isolated_services.compute.create_unit(
        UnitName("rollout-contention"), workspace=workspace_id, provider="agent"
    )
    guard = _RecordingCapacityReservationGuard(open_reservations=False)
    gateway = replace(
        _gateway(isolated_services, guard, key_prefix="rollout-contention"),
        agent_worker_image="worker:target",
    )
    join = gateway.unit_state_coordinator.create_unit_join_token(
        unit, workspace_id=workspace_id, owner_token_id="gateway-test-owner"
    )
    enrolled = gateway.join_agent(_join_request(join.token))
    state = gateway._agent_state_for_token(enrolled.agent_token)
    assert state is not None
    worker_id = agent_machine_worker_id(enrolled.machine_id)
    workers = RedisSchedulerWorkerRepository(gateway.compute_state.redis)
    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id=worker_id,
            machine_id=enrolled.machine_id,
            capacity_owner_id=unit.capacity_owner_id,
            pool=unit.pool,
            status=worker_status,
        )
    )

    def contended(
        self: _RecordingCapacityReservationGuard, capacity_owner_id: str
    ) -> AbstractContextManager[None]:
        del self, capacity_owner_id
        raise CapacityReservationLockContendedError("another reconciler owns capacity")

    monkeypatch.setattr(_RecordingCapacityReservationGuard, "mutation_lock", contended)
    slots = gateway._agent_slots_for_machine(
        state,
        billing_owner=billing_owner_for_unit(unit),
        active_worker_images={worker_id: "worker:current"},
        rollout_fleet_size=1,
    )
    assert len(slots) == 1 and slots[0].status is slot_status
    current = workers.get_worker(worker_id)
    assert current is not None and current.status is worker_status


def test_a_connected_cloud_unit_bills_its_machines_differently_than_a_brought_one(
    isolated_services: ApiServices,
) -> None:
    """The management fee turns on one field, so that field decides alone.

    A unit holding a provider connection was provisioned into a customer's own
    cloud account; a unit without one is hardware somebody brought and carried to
    us. `ComputeUnitRecord` refuses a connection on any unit that is not
    internal, so the two cases below are the only two a joined machine can
    resolve to.
    """

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    brought = isolated_services.compute.create_unit(
        UnitName("brought-unit"),
        workspace=workspace_id,
        provider="agent",
        capacity_owner_id=str(uuid4()),
        max_machines=1,
    )
    connected = brought.model_copy(
        update={
            "visibility": ComputeUnitVisibility.Internal,
            "provider_connection_id": str(uuid4()),
        }
    )

    assert billing_owner_for_unit(brought) is UsageBillingOwner.SelfHosted
    assert billing_owner_for_unit(connected) is UsageBillingOwner.ConnectedCloud
