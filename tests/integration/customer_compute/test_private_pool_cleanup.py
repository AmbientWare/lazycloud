from __future__ import annotations

from dataclasses import replace

from api.server.services import ApiServices
from compute.offers import ComputeOffer
from compute.state import (
    ComputeAgentTokenState,
    ComputeAgentWorkerSlotState,
    ComputePoolState,
    RedisComputeStateRepository,
)
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
from shared.routing import AgentBackendRoute
from tests.provider_fixtures import RecordingDirectMachineProvider, configure_test_provider
from tests.real_redis import RealRedisActors

_FOREIGN_CAPACITY_OWNER_ID = "11111111-1111-4111-8111-111111111111"


def _test_provider(isolated_services: ApiServices) -> RecordingDirectMachineProvider:
    return configure_test_provider(
        isolated_services,
        "generic",
        [
            ComputeOffer(
                id="cpu-small",
                provider="generic",
                instance_type="cpu-small",
                region="local",
                cpu_millicores=1000,
                memory_mb=1024,
                hourly_cost_micros=1_000_000,
                available=1,
            )
        ],
    )


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
    pool = isolated_services.compute.create_pool("cleanup-pool", provider="local")
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    scheduler_workers = RedisSchedulerWorkerRepository(redis)
    scheduler_containers = RedisSchedulerContainerRepository(redis)
    scheduler_pool_states = RedisWorkerPoolStateRepository(redis)
    scheduler_pool_states.set_state(
        pool.capacity_owner_id,
        WorkerPoolStateSnapshot(
            capacity_owner_id=pool.capacity_owner_id,
            pool_name=pool.name,
        ),
    )
    scheduler_replicas_key = scheduler_pool_states.keys.worker_pool_replicas(pool.capacity_owner_id)
    redis.set(scheduler_replicas_key, "replica-state")
    compute_states.save_pool_state(
        ComputePoolState(
            workspace_id=workspace_id,
            name="cleanup-pool",
            capacity_owner_id=pool.capacity_owner_id,
            provider="local",
        )
    )
    compute_states.save_agent_token_state(
        ComputeAgentTokenState(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            token_hash="agent-hash",
            workspace_id=workspace_id,
            pool_name="cleanup-pool",
            machine_id="machine-one",
        )
    )
    compute_states.save_agent_worker_slot_state(
        ComputeAgentWorkerSlotState(
            capacity_owner_id=pool.capacity_owner_id,
            workspace_id=workspace_id,
            pool_name="cleanup-pool",
            machine_id="machine-one",
            worker_id="worker-one",
        )
    )
    compute_states.save_agent_route_state(
        AgentBackendRoute(
            route_id="route-one",
            workspace_id=workspace_id,
            pool_name="cleanup-pool",
            machine_id="machine-one",
            worker_id="worker-one",
            container_id="container-one",
            port=8001,
        )
    )
    scheduler_workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id=pool.capacity_owner_id,
            worker_id="worker-one",
            pool_name="cleanup-pool",
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
            pool_name="cleanup-pool",
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

    gateway.delete_pool("cleanup-pool", workspace_id=workspace_id)

    assert compute_states.get_pool_state(workspace_id, "cleanup-pool") is None
    assert compute_states.get_agent_token_state("agent-hash") is None
    assert compute_states.list_agent_token_states(workspace_id, "cleanup-pool") == []
    assert (
        compute_states.list_agent_worker_slot_states(
            workspace_id,
            "cleanup-pool",
            "machine-one",
        )
        == []
    )
    assert compute_states.list_agent_route_states(workspace_id, "cleanup-pool", "machine-one") == []
    assert [
        worker.worker_id for worker in scheduler_workers.list_workers_in_pool("cleanup-pool")
    ] == ["worker-foreign"]
    assert not scheduler_pool_states.delete_pool_state(pool.capacity_owner_id)
    assert redis.get(scheduler_replicas_key) is None
