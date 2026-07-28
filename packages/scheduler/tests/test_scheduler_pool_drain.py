from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from api.server.services import ApiServices
from compute.offers import ComputeOffer
from compute.projection import PoolConfig
from compute.state import RedisComputeStateRepository
from coordination.redis_client import RedisClient
from scheduler.capacity_reservations import (
    CapacityRequestShape,
    CapacityReservationService,
    RedisCapacityReservationRepository,
)
from scheduler.compute_hooks import SchedulerComputeHooks
from scheduler.fleet import SchedulerWorkerStatus
from scheduler.pool_drain import (
    WorkerPoolDrainAction,
    WorkerPoolDrainService,
    managed_compute_drain_controllers,
)
from scheduler.service import Scheduler, SchedulerCapacityControls
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerPoolStateRepository,
    SchedulerWorkerRecord,
)
from shared.capacity import CapacityOwnerKind
from shared.scheduling import SchedulerWorkerRequest
from tests.metric_helpers import metric_value
from tests.provider_fixtures import configure_test_provider


class _RealRedisActors(Protocol):
    def client(self) -> RedisClient: ...


def test_scheduler_records_worker_pool_drain_events_and_metrics(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    locks = RedisWorkerPoolStateRepository(redis)
    isolated_services.compute.scheduler_hooks = SchedulerComputeHooks(compute_states, workers)
    configure_test_provider(
        isolated_services,
        "generic",
        [
            ComputeOffer(
                id="cpu-small",
                provider="generic",
                instance_type="cpu-small",
                region="lab",
                cpu_millicores=2000,
                memory_mb=4096,
                hourly_cost_micros=250_000,
                available=2,
            )
        ],
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    launched = isolated_services.compute.launch_pool_capacity(
        PoolConfig(name="cpu", providers=["generic"], nodes=2, ttl="1h", max_spend=2.0),
        now=now,
    )
    recorded = compute_states.get_pool_state(launched.workspace_id, "cpu")
    assert recorded is not None
    compute_states.save_pool_state(
        recorded.model_copy(
            update={
                "min_machines": 1,
                "metadata": {
                    **recorded.metadata,
                    "drain": {"scale_down_idle_seconds": "10"},
                },
            }
        )
    )
    for reservation in launched.reservations:
        _add_worker(
            workers,
            f"worker-{reservation.machine_id}",
            now - timedelta(seconds=30),
            pool_name="cpu",
            machine_id=reservation.machine_id,
            capacity_owner_id=recorded.capacity_owner_id,
        )
    scheduler = Scheduler(
        isolated_services,
        capacity=SchedulerCapacityControls(
            worker_pool_drain=WorkerPoolDrainService(
                redis,
                locks,
                lambda: managed_compute_drain_controllers(
                    isolated_services.compute,
                    compute_states,
                    workers,
                    containers,
                ),
                _reservation_service(redis),
            )
        ),
        reconcile_agent_pools_enabled=False,
    )

    results = scheduler.drain_worker_pools(now=now)

    assert results[0].action is WorkerPoolDrainAction.TerminateProviderMachine
    event = next(
        item
        for item in isolated_services.events.list()
        if item.action == "worker_pool.drain.decision"
    )
    assert event.resource_type == "worker_pool"
    assert event.resource_id == "cpu"
    assert event.data["action"] == "terminate-provider-machine"
    snapshot = isolated_services.metrics.latest()
    labels = {"source": "worker_pool.drain", "pool_name": "cpu"}
    assert (
        metric_value(
            snapshot.counters,
            "worker_pool_drain_decisions_total",
            **labels,
            action="terminate-provider-machine",
        )
        == 1
    )
    assert metric_value(snapshot.gauges, "worker_pool_desired_replicas", **labels) == 1
    assert metric_value(snapshot.gauges, "worker_pool_observed_replicas", **labels) == 1


def test_worker_pool_drain_terminates_idle_managed_provider_machine(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    locks = RedisWorkerPoolStateRepository(redis)
    isolated_services.compute.scheduler_hooks = SchedulerComputeHooks(compute_states, workers)
    provider = configure_test_provider(
        isolated_services,
        "generic",
        [
            ComputeOffer(
                id="cpu-small",
                provider="generic",
                instance_type="cpu-small",
                region="lab",
                cpu_millicores=2000,
                memory_mb=4096,
                hourly_cost_micros=250_000,
                available=2,
            )
        ],
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    launched = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="cpu",
            providers=["generic"],
            nodes=2,
            ttl="1h",
            max_spend=2.0,
        ),
        now=now,
    )
    recorded = compute_states.get_pool_state(launched.workspace_id, "cpu")
    assert recorded is not None
    compute_states.save_pool_state(
        recorded.model_copy(
            update={
                "min_machines": 1,
                "metadata": {
                    **recorded.metadata,
                    "drain": {"scale_down_idle_seconds": "10"},
                },
            }
        )
    )
    capacity_owner_id = recorded.capacity_owner_id
    for reservation in launched.reservations:
        _add_worker(
            workers,
            f"worker-{reservation.machine_id}",
            now - timedelta(seconds=30),
            pool_name="cpu",
            machine_id=reservation.machine_id,
            capacity_owner_id=capacity_owner_id,
        )
    reservations = _reservation_service(redis)
    _reserve_allocation(
        reservations.reservations,
        capacity_owner_id=capacity_owner_id,
        pool_name="cpu",
        owner_kind=CapacityOwnerKind.ManagedPool,
        container_id="container-awaiting-provider-capacity",
        now=now,
    )
    service = WorkerPoolDrainService(
        redis,
        locks,
        lambda: managed_compute_drain_controllers(
            isolated_services.compute,
            compute_states,
            workers,
            containers,
        ),
        reservations,
    )

    blocked = service.reconcile(now=now)
    assert blocked[0].action is WorkerPoolDrainAction.None_
    assert blocked[0].reason == "capacity owner has open provisioning allocations"
    assert len(provider.list_machines("cpu")) == 2

    reservations.release_request(
        "container-awaiting-provider-capacity",
        now=now,
    )
    result = service.reconcile(now=now)

    assert result[0].action is WorkerPoolDrainAction.TerminateProviderMachine
    assert result[0].capacity_owner_id == capacity_owner_id
    assert result[0].machine_id in {reservation.machine_id for reservation in launched.reservations}
    assert result[0].desired_replicas == 1
    assert len(provider.list_machines("cpu")) == 1
    updated = compute_states.get_pool_state(launched.workspace_id, "cpu")
    assert updated is not None
    assert updated.active_machines == 1


def _reservation_service(redis: RedisClient) -> CapacityReservationService:
    return CapacityReservationService(RedisCapacityReservationRepository(redis), lambda: [])


def _reserve_allocation(
    reservations: RedisCapacityReservationRepository,
    *,
    capacity_owner_id: str,
    pool_name: str,
    owner_kind: CapacityOwnerKind,
    container_id: str,
    now: datetime,
) -> None:
    reservations.reserve(
        capacity_owner_id=capacity_owner_id,
        pool_name=pool_name,
        owner_kind=owner_kind,
        request=SchedulerWorkerRequest(
            workspace_id="workspace",
            stub_id="stub",
            container_id=container_id,
            cpu_millicores=100,
            memory_mib=128,
            pool_selector=pool_name,
            capacity_owner_id=capacity_owner_id,
        ),
        shape=CapacityRequestShape(cpu_millicores=1000, memory_mib=1024),
        registration_timeout=timedelta(seconds=600),
        now=now,
    )


def _add_worker(
    workers: RedisSchedulerWorkerRepository,
    worker_id: str,
    updated_at: datetime,
    *,
    pool_name: str,
    machine_id: str,
    capacity_owner_id: str,
) -> None:
    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id=worker_id,
            pool_name=pool_name,
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
