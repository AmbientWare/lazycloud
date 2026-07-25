from __future__ import annotations

from dataclasses import replace

import pytest
from api.server.services import ApiServices
from compute.billing import BillingCreditRequest, BillingDecision, ManagedUsage
from compute.offers import ComputeOffer
from compute.projection import PoolConfig
from compute.state import (
    ComputeAgentRouteState,
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
from shared.errors import UpstreamUnavailableError
from tests.provider_fixtures import RecordingDirectMachineProvider, configure_test_provider
from tests.real_redis import RealRedisActors


class _BillingRecorder:
    def __init__(self) -> None:
        self.usage: list[ManagedUsage] = []

    def check_launch_credit(self, request: BillingCreditRequest) -> BillingDecision:
        del request
        return BillingDecision(ok=True, available_cents=1000)

    def check_balance(self, workspace_id: str) -> BillingDecision:
        del workspace_id
        return BillingDecision(ok=True, available_cents=1000)

    def record_usage(self, usage: ManagedUsage) -> None:
        self.usage.append(usage)


def _launch_managed_pool(
    isolated_services: ApiServices,
) -> _BillingRecorder:
    billing = _BillingRecorder()
    isolated_services.compute.billing = billing
    isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="durable-only",
            providers=["generic"],
            nodes=1,
            ttl="10m",
            max_spend=1.0,
        ),
    )
    return billing


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
            token_hash="agent-hash",
            workspace_id=workspace_id,
            pool_name="cleanup-pool",
            machine_id="machine-one",
        )
    )
    compute_states.save_agent_worker_slot_state(
        ComputeAgentWorkerSlotState(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            workspace_id=workspace_id,
            pool_name="cleanup-pool",
            machine_id="machine-one",
            worker_id="worker-one",
        )
    )
    compute_states.save_agent_route_state(
        ComputeAgentRouteState(
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
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
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
    assert scheduler_workers.list_workers_in_pool("cleanup-pool") == []
    assert not scheduler_pool_states.delete_pool_state(pool.capacity_owner_id)
    assert redis.get(scheduler_replicas_key) is None


def test_delete_pool_removes_durable_pool_when_redis_projection_is_missing(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    provider = _test_provider(isolated_services)
    billing = _launch_managed_pool(isolated_services)
    redis = real_redis_actors.client()
    gateway = replace(
        isolated_services.gateway_service,
        compute_state=RedisComputeStateRepository(redis),
        capacity_reservations=_capacity_reservations(redis),
    )

    gateway.delete_pool("durable-only", workspace_id=workspace_id)

    assert all(
        pool.name != "durable-only"
        for pool in isolated_services.compute.list_pools(workspace=workspace_id)
    )
    assert isolated_services.compute.get_private_pool_state("durable-only") is None
    assert provider.list_machines("durable-only") == []
    assert all(
        machine.status.value == "deleted"
        for machine in isolated_services.compute.list_machines(workspace=workspace_id)
        if machine.pool == "durable-only"
    )
    assert billing.usage


def test_delete_pool_retains_durable_ownership_when_provider_cleanup_fails(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    provider = _test_provider(isolated_services)
    billing = _launch_managed_pool(isolated_services)

    def fail_termination(machine_id: str) -> None:
        del machine_id
        raise RuntimeError("provider termination failed")

    monkeypatch.setattr(provider, "terminate_machine", fail_termination)
    redis = real_redis_actors.client()
    gateway = replace(
        isolated_services.gateway_service,
        compute_state=RedisComputeStateRepository(redis),
        capacity_reservations=_capacity_reservations(redis),
    )

    try:
        gateway.delete_pool(
            "durable-only",
            workspace_id=workspace_id,
        )
    except UpstreamUnavailableError as exc:
        assert "provider termination failed" in str(exc)
    else:
        raise AssertionError("expected provider cleanup failure")

    assert any(
        pool.name == "durable-only"
        for pool in isolated_services.compute.list_pools(workspace=workspace_id)
    )
    assert isolated_services.compute.get_private_pool_state("durable-only") is not None
    assert provider.list_machines("durable-only")
    assert billing.usage
