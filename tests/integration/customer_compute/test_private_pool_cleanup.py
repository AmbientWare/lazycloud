from __future__ import annotations

from dataclasses import replace

from api.server.services import ApiServices
from compute.state import ComputeUnitState, RedisComputeStateRepository
from coordination.redis_client import RedisClient
from scheduler.capacity_reservations import (
    CapacityReservationService,
    RedisCapacityReservationRepository,
)
from scheduler.fleet import SchedulerWorkerStatus, WorkerPoolStateSnapshot
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerPoolStateRepository,
    SchedulerWorkerRecord,
)
from shared.compute_policy import MachinePool, UnitName
from tests.real_redis import RealRedisActors

_FOREIGN_CAPACITY_OWNER_ID = "11111111-1111-4111-8111-111111111111"


def _capacity_reservations(redis: RedisClient) -> CapacityReservationService:
    return CapacityReservationService(
        RedisCapacityReservationRepository(redis),
        lambda: [],
    )


def test_delete_pool_cleans_private_agent_state(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    unit = isolated_services.compute.create_unit(UnitName("cleanup-pool"), provider="local")
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    scheduler_workers = RedisSchedulerWorkerRepository(redis)
    scheduler_containers = RedisSchedulerContainerRepository(redis)
    scheduler_pool_states = RedisWorkerPoolStateRepository(redis)
    scheduler_pool_states.set_state(
        unit.capacity_owner_id,
        WorkerPoolStateSnapshot(
            capacity_owner_id=unit.capacity_owner_id,
            pool=unit.pool,
        ),
    )
    scheduler_replicas_key = scheduler_pool_states.keys.worker_pool_replicas(unit.capacity_owner_id)
    redis.set(scheduler_replicas_key, "replica-state")
    compute_states.save_unit_state(
        ComputeUnitState(
            workspace_id=workspace_id,
            name=UnitName("cleanup-pool"),
            capacity_owner_id=unit.capacity_owner_id,
            provider="local",
        )
    )
    scheduler_workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id=unit.capacity_owner_id,
            worker_id="worker-one",
            pool=MachinePool("cleanup-pool"),
            machine_id="machine-one",
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            requires_pool_selector=True,
        )
    )
    # A worker owned by a different pool must survive this deletion even though
    # it reports the same pool name.
    scheduler_workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id=_FOREIGN_CAPACITY_OWNER_ID,
            worker_id="worker-foreign",
            pool=MachinePool("cleanup-pool"),
            machine_id="machine-two",
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            requires_pool_selector=True,
        )
    )
    gateway = replace(
        isolated_services.gateway_service,
        compute_state=compute_states,
        scheduler_workers=scheduler_workers,
        scheduler_containers=scheduler_containers,
        scheduler_pool_states=scheduler_pool_states,
        capacity_reservations=_capacity_reservations(redis),
    )

    gateway.delete_unit(unit.id, workspace_id=workspace_id)

    assert compute_states.get_unit_state(workspace_id, unit.capacity_owner_id) is None
    assert [
        worker.worker_id for worker in scheduler_workers.list_workers_in_pool("cleanup-pool")
    ] == ["worker-foreign"]
    assert not scheduler_pool_states.delete_unit_state(unit.capacity_owner_id)
    assert redis.get(scheduler_replicas_key) is None
