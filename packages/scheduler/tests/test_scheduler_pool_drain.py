from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from compute.providers import (
    ProviderCapacityPhase,
    ProviderUnitInstance,
    ProviderUnitSnapshot,
)
from compute.state import ComputeUnitState, RedisComputeStateRepository
from coordination.redis_client import RedisClient
from scheduler.capacity_reservations import (
    CapacityReservationService,
    RedisCapacityReservationRepository,
)
from scheduler.fleet import SchedulerWorkerStatus
from scheduler.pool_drain import (
    WorkerPoolDrainAction,
    WorkerPoolDrainService,
    managed_compute_drain_controllers,
)
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    SchedulerWorkerRecord,
)
from shared.capacity import CapacityPoolSizingSnapshot
from shared.compute_policy import (
    ComputeUnitRecord,
    MachinePool,
    UnitName,
)
from shared.scheduling import SchedulerContainerState, SchedulerContainerStatus

WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
PROVIDER_OWNER_ID = "11111111-1111-4111-8111-111111111111"
JOINED_OWNER_ID = str(uuid5(NAMESPACE_URL, "joined-fleet"))
POOL = "lazycloud"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _RealRedisActors(Protocol):
    def client(self) -> RedisClient: ...


@dataclass(slots=True)
class _Compute:
    """The two calls the drain makes, recorded.

    The drain reads its pools from hot state and its workers from the worker
    repository; compute is reached only to size a pool and to release the
    machine finally chosen. Recording that choice is what these tests assert.
    """

    released: list[tuple[str, str]] = field(default_factory=list)
    cordoned: list[str] = field(default_factory=list)
    scaled: list[int] = field(default_factory=list)
    current_template_version: str = ""
    instances: list[tuple[str, str]] = field(default_factory=list)
    """Provider instance id and the template version it booted with."""
    cordoned_since: dict[str, datetime] = field(default_factory=dict)
    """Machine id to when it stopped accepting work, as the enrollment records it."""

    def pool_sizing_snapshot(self, capacity_owner_id: str) -> CapacityPoolSizingSnapshot:
        return CapacityPoolSizingSnapshot(capacity_owner_id=capacity_owner_id)

    def inspect_internal_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> tuple[ComputeUnitRecord, ProviderUnitSnapshot]:
        _ = workspace_id, capacity_owner_id
        return (
            ComputeUnitRecord(
                id=PROVIDER_OWNER_ID,
                capacity_owner_id=PROVIDER_OWNER_ID,
                workspace_id=WORKSPACE_ID,
                name=UnitName(POOL),
                pool=MachinePool(POOL),
            ),
            ProviderUnitSnapshot(
                phase=ProviderCapacityPhase.Ready,
                current_template_version=self.current_template_version,
                instances=[
                    ProviderUnitInstance(
                        provider_instance_id=instance_id,
                        booted_template_version=version,
                    )
                    for instance_id, version in self.instances
                ],
            ),
        )

    def get_internal_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> ComputeUnitRecord:
        unit, _ = self.inspect_internal_unit(workspace_id, capacity_owner_id)
        return unit

    def internal_unit_cordoned_machines(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> dict[str, datetime]:
        _ = workspace_id, capacity_owner_id
        return dict(self.cordoned_since)

    def internal_unit_machine_by_instance(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> dict[str, str]:
        _ = workspace_id, capacity_owner_id
        return {instance_id: f"machine-{instance_id}" for instance_id, _ in self.instances}

    def scale_internal_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        desired_machines: int,
        *,
        before_mutation: object = None,
        now: datetime | None = None,
    ) -> ComputeUnitRecord:
        _ = workspace_id, capacity_owner_id, before_mutation, now
        self.scaled.append(desired_machines)
        return ComputeUnitRecord(
            id=PROVIDER_OWNER_ID,
            capacity_owner_id=PROVIDER_OWNER_ID,
            workspace_id=WORKSPACE_ID,
            name=UnitName(POOL),
            pool=MachinePool(POOL),
            desired_machines=desired_machines,
            max_machines=max(desired_machines, 4),
        )

    def cordon_internal_unit_machine(
        self,
        workspace_id: str,
        machine_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        _ = workspace_id, reason, now
        if machine_id in self.cordoned:
            return False
        self.cordoned.append(machine_id)
        self.cordoned_since[machine_id] = now or NOW
        return True

    def release_internal_unit_machine(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
    ) -> ComputeUnitRecord:
        _ = workspace_id
        self.released.append((capacity_owner_id, machine_id))
        return ComputeUnitRecord(
            id=PROVIDER_OWNER_ID,
            capacity_owner_id=PROVIDER_OWNER_ID,
            workspace_id=WORKSPACE_ID,
            name=UnitName(POOL),
            pool=MachinePool(POOL),
            desired_machines=0,
            observed_machines=0,
        )


def _seed_pool_state(
    compute_states: RedisComputeStateRepository,
    *,
    capacity_owner_id: str,
    active_machines: int,
    min_machines: int = 0,
) -> None:
    compute_states.save_unit_state(
        ComputeUnitState(
            workspace_id=WORKSPACE_ID,
            name=UnitName(POOL),
            capacity_owner_id=capacity_owner_id,
            provider="aws",
            min_machines=min_machines,
            max_machines=4,
            desired_machines=active_machines,
            active_machines=active_machines,
            metadata={
                "config": {"name": POOL, "providers": ["aws"]},
                "drain": {"scale_down_idle_seconds": "10"},
            },
        )
    )


def _add_worker(
    workers: RedisSchedulerWorkerRepository,
    worker_id: str,
    updated_at: datetime,
    *,
    machine_id: str,
    capacity_owner_id: str,
) -> None:
    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id=worker_id,
            pool=MachinePool(POOL),
            capacity_owner_id=capacity_owner_id,
            machine_id=machine_id,
            status=SchedulerWorkerStatus.Available,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
            created_at=updated_at,
            updated_at=updated_at,
        ),
        now=updated_at,
    )


def _add_container(redis: RedisClient, container_id: str, worker_id: str) -> None:
    RedisSchedulerContainerRepository(redis).set_container_state(
        SchedulerContainerState(
            container_id=container_id,
            stub_id="stub-1",
            workspace_id=WORKSPACE_ID,
            worker_id=worker_id,
            status=SchedulerContainerStatus.Running,
        )
    )


def _drain_service(
    redis: RedisClient,
    compute: _Compute,
    compute_states: RedisComputeStateRepository,
    workers: RedisSchedulerWorkerRepository,
) -> WorkerPoolDrainService:
    return WorkerPoolDrainService(
        lambda: managed_compute_drain_controllers(
            compute,  # pyright: ignore[reportArgumentType]
            compute_states,
            workers,
            RedisSchedulerContainerRepository(redis),
        ),
        CapacityReservationService(RedisCapacityReservationRepository(redis), lambda: []),
    )


def test_worker_pool_drain_releases_the_idle_provider_machine(
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    compute = _Compute()
    _seed_pool_state(compute_states, capacity_owner_id=PROVIDER_OWNER_ID, active_machines=1)
    _add_worker(
        workers,
        "worker-provider",
        NOW - timedelta(seconds=30),
        machine_id="machine-provider",
        capacity_owner_id=PROVIDER_OWNER_ID,
    )

    result = _drain_service(redis, compute, compute_states, workers).reconcile(now=NOW)

    assert [item.action for item in result] == [WorkerPoolDrainAction.TerminateProviderMachine]
    assert compute.released == [(PROVIDER_OWNER_ID, "machine-provider")]


def test_worker_pool_drain_holds_at_the_pool_minimum(
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    compute = _Compute()
    _seed_pool_state(
        compute_states,
        capacity_owner_id=PROVIDER_OWNER_ID,
        active_machines=1,
        min_machines=1,
    )
    _add_worker(
        workers,
        "worker-provider",
        NOW - timedelta(seconds=30),
        machine_id="machine-provider",
        capacity_owner_id=PROVIDER_OWNER_ID,
    )

    result = _drain_service(redis, compute, compute_states, workers).reconcile(now=NOW)

    assert [item.action for item in result] == [WorkerPoolDrainAction.None_]
    assert compute.released == []


def test_drain_never_releases_a_machine_another_unit_owns(
    real_redis_actors: _RealRedisActors,
) -> None:
    """A joined machine sharing a pool with an auto-scaling unit survives its drain.

    Once several units feed one pool, the pool label no longer identifies who
    bought a machine. The drain selects candidates by capacity owner for exactly
    this reason: without that co-filter, scaling the provider unit down would
    release a host the customer joined themselves. The joined worker is idle
    longest here, so it is the candidate ordering alone would pick.
    """
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    compute = _Compute()
    _seed_pool_state(compute_states, capacity_owner_id=PROVIDER_OWNER_ID, active_machines=1)
    _add_worker(
        workers,
        "worker-joined-host",
        NOW - timedelta(seconds=600),
        machine_id="machine-joined-host",
        capacity_owner_id=JOINED_OWNER_ID,
    )
    _add_worker(
        workers,
        "worker-provider",
        NOW - timedelta(seconds=30),
        machine_id="machine-provider",
        capacity_owner_id=PROVIDER_OWNER_ID,
    )

    result = _drain_service(redis, compute, compute_states, workers).reconcile(now=NOW)

    assert [item.action for item in result] == [WorkerPoolDrainAction.TerminateProviderMachine]
    assert compute.released == [(PROVIDER_OWNER_ID, "machine-provider")]
    assert workers.get_worker("worker-joined-host") is not None


def test_replacement_surges_before_it_cordons_anything(
    real_redis_actors: _RealRedisActors,
) -> None:
    """The replacement is added first, and nothing is taken away in the same pass.

    Cordoning first would drop a pool at its minimum to no capacity at all, and
    the idle-drain phase below refuses to go under that floor for exactly that
    reason.
    """
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    compute = _Compute(current_template_version="2", instances=[("i-old", "1")])
    _seed_pool_state(
        compute_states,
        capacity_owner_id=PROVIDER_OWNER_ID,
        active_machines=1,
        min_machines=1,
    )
    _add_worker(
        workers,
        "worker-old",
        NOW,
        machine_id="machine-i-old",
        capacity_owner_id=PROVIDER_OWNER_ID,
    )

    result = _drain_service(redis, compute, compute_states, workers).reconcile(now=NOW)

    assert [item.action for item in result] == [WorkerPoolDrainAction.SurgeReplacementMachine]
    assert compute.scaled == [2]
    assert compute.cordoned == []
    assert compute.released == []


def test_replacement_cordons_only_once_the_surge_has_registered(
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    compute = _Compute(
        current_template_version="2",
        instances=[("i-old", "1"), ("i-new", "2")],
    )
    _seed_pool_state(
        compute_states,
        capacity_owner_id=PROVIDER_OWNER_ID,
        active_machines=2,
        min_machines=1,
    )
    for worker_id, machine_id in (("worker-old", "machine-i-old"), ("worker-new", "machine-i-new")):
        _add_worker(
            workers,
            worker_id,
            NOW,
            machine_id=machine_id,
            capacity_owner_id=PROVIDER_OWNER_ID,
        )

    result = _drain_service(redis, compute, compute_states, workers).reconcile(now=NOW)

    assert [item.action for item in result] == [WorkerPoolDrainAction.CordonSupersededMachine]
    assert compute.cordoned == ["machine-i-old"]
    assert compute.released == []


def test_replacement_does_nothing_when_the_provider_reports_no_version(
    real_redis_actors: _RealRedisActors,
) -> None:
    """No version is "cannot tell", never "everything is stale".

    Read the other way, a provider that reports nothing would have its whole pool
    replaced on the first pass. The same pool with a version set is what makes
    that a decision about the empty string rather than about the wiring.
    """
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    _seed_pool_state(
        compute_states,
        capacity_owner_id=PROVIDER_OWNER_ID,
        active_machines=1,
        min_machines=1,
    )
    _add_worker(
        workers,
        "worker-old",
        NOW,
        machine_id="machine-i-old",
        capacity_owner_id=PROVIDER_OWNER_ID,
    )

    silent = _Compute(current_template_version="", instances=[("i-old", "1")])
    _drain_service(redis, silent, compute_states, workers).reconcile(now=NOW)

    speaking = _Compute(current_template_version="2", instances=[("i-old", "1")])
    _drain_service(redis, speaking, compute_states, workers).reconcile(now=NOW)

    assert silent.scaled == []
    assert speaking.scaled == [2]


def test_replacement_releases_a_cordoned_machine_once_it_is_empty(
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    compute = _Compute(
        current_template_version="2",
        instances=[("i-old", "1"), ("i-new", "2")],
    )
    _seed_pool_state(
        compute_states,
        capacity_owner_id=PROVIDER_OWNER_ID,
        active_machines=2,
        min_machines=1,
    )
    _add_worker(
        workers,
        "worker-new",
        NOW,
        machine_id="machine-i-new",
        capacity_owner_id=PROVIDER_OWNER_ID,
    )
    _add_worker(
        workers,
        "worker-old",
        NOW,
        machine_id="machine-i-old",
        capacity_owner_id=PROVIDER_OWNER_ID,
    )
    compute.cordoned_since["machine-i-old"] = NOW - timedelta(minutes=5)

    result = _drain_service(redis, compute, compute_states, workers).reconcile(now=NOW)

    assert [item.action for item in result] == [WorkerPoolDrainAction.TerminateProviderMachine]
    assert compute.released == [(PROVIDER_OWNER_ID, "machine-i-old")]


def test_replacement_cordons_one_machine_while_another_is_in_flight(
    real_redis_actors: _RealRedisActors,
) -> None:
    """A template change must not cordon a whole pool at once.

    Every cordoned machine costs a surged replacement, so cordoning three at once
    would ask for three extra nodes and take three machines' placement capacity
    away before any of them arrived.
    """
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    compute = _Compute(
        current_template_version="2",
        instances=[("i-a", "1"), ("i-b", "1"), ("i-new", "2")],
        cordoned_since={"machine-i-a": NOW},
    )
    _seed_pool_state(
        compute_states,
        capacity_owner_id=PROVIDER_OWNER_ID,
        active_machines=3,
        min_machines=1,
    )
    for machine_id in ("machine-i-a", "machine-i-b", "machine-i-new"):
        _add_worker(
            workers,
            f"worker-{machine_id}",
            NOW,
            machine_id=machine_id,
            capacity_owner_id=PROVIDER_OWNER_ID,
        )

    _drain_service(redis, compute, compute_states, workers).reconcile(now=NOW)

    # The one already cordoned is dealt with; the second stale machine is left alone.
    assert compute.cordoned == []
    assert compute.released == [(PROVIDER_OWNER_ID, "machine-i-a")]


def test_replacement_releases_a_cordoned_machine_once_its_deadline_passes(
    real_redis_actors: _RealRedisActors,
) -> None:
    """A container that cannot be requeued must not pin a node on an old release.

    The deadline runs from the cordon, not from the worker's heartbeat, which
    keeps moving for as long as the machine is alive.
    """
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    compute = _Compute(
        current_template_version="2",
        instances=[("i-old", "1"), ("i-new", "2")],
        cordoned_since={"machine-i-old": NOW - timedelta(hours=2)},
    )
    _seed_pool_state(
        compute_states,
        capacity_owner_id=PROVIDER_OWNER_ID,
        active_machines=2,
        min_machines=1,
    )
    for machine_id in ("machine-i-old", "machine-i-new"):
        _add_worker(
            workers,
            f"worker-{machine_id}",
            NOW,
            machine_id=machine_id,
            capacity_owner_id=PROVIDER_OWNER_ID,
        )
    _add_container(redis, "container-stuck", "worker-machine-i-old")

    result = _drain_service(redis, compute, compute_states, workers).reconcile(now=NOW)

    assert [item.action for item in result] == [WorkerPoolDrainAction.TerminateProviderMachine]
    assert compute.released == [(PROVIDER_OWNER_ID, "machine-i-old")]


def test_replacement_holds_a_cordoned_machine_inside_its_deadline(
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    compute = _Compute(
        current_template_version="2",
        instances=[("i-old", "1"), ("i-new", "2")],
        cordoned_since={"machine-i-old": NOW - timedelta(minutes=5)},
    )
    _seed_pool_state(
        compute_states,
        capacity_owner_id=PROVIDER_OWNER_ID,
        active_machines=2,
        min_machines=1,
    )
    for machine_id in ("machine-i-old", "machine-i-new"):
        _add_worker(
            workers,
            f"worker-{machine_id}",
            NOW,
            machine_id=machine_id,
            capacity_owner_id=PROVIDER_OWNER_ID,
        )
    _add_container(redis, "container-running", "worker-machine-i-old")

    result = _drain_service(redis, compute, compute_states, workers).reconcile(now=NOW)

    assert compute.released == []
    assert [item.reason for item in result] == ["cordoned machine is still draining"]
