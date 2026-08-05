from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from compute.state import ComputePoolState, RedisComputeStateRepository
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
    RedisWorkerPoolStateRepository,
    SchedulerWorkerRecord,
)
from shared.capacity import CapacityPoolSizingSnapshot
from shared.compute_policy import ComputePoolRecord

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

    def pool_sizing_snapshot(self, capacity_owner_id: str) -> CapacityPoolSizingSnapshot:
        return CapacityPoolSizingSnapshot(capacity_owner_id=capacity_owner_id)

    def release_internal_pool_machine(
        self,
        workspace_id: str,
        pool_name: str,
        machine_id: str,
    ) -> ComputePoolRecord:
        _ = workspace_id
        self.released.append((pool_name, machine_id))
        return ComputePoolRecord(
            id=PROVIDER_OWNER_ID,
            capacity_owner_id=PROVIDER_OWNER_ID,
            workspace_id=WORKSPACE_ID,
            name=pool_name,
            machine_pool=POOL,
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
    compute_states.save_pool_state(
        ComputePoolState(
            workspace_id=WORKSPACE_ID,
            name=POOL,
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
            pool_name=POOL,
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


def _drain_service(
    redis: RedisClient,
    compute: _Compute,
    compute_states: RedisComputeStateRepository,
    workers: RedisSchedulerWorkerRepository,
) -> WorkerPoolDrainService:
    return WorkerPoolDrainService(
        redis,
        RedisWorkerPoolStateRepository(redis),
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
    assert compute.released == [(POOL, "machine-provider")]


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
    assert compute.released == [(POOL, "machine-provider")]
    assert workers.get_worker("worker-joined-host") is not None
