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
from scheduler.fleet import SchedulerContainerStatus, SchedulerWorkerStatus
from scheduler.pool_drain import (
    StaticWorkerPoolDrainController,
    WorkerPoolDrainAction,
    WorkerPoolDrainService,
    managed_compute_drain_controllers,
    static_worker_pool_drain_controllers,
)
from scheduler.pool_sizing import (
    RedisWorkerPoolReplicaStateStore,
    WorkerPoolReplicaScaleOutcome,
    WorkerPoolReplicaScaleResult,
    WorkerPoolReplicaState,
)
from scheduler.service import Scheduler, SchedulerCapacityControls
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerPoolStateRepository,
    SchedulerContainerState,
    SchedulerWorkerRecord,
)
from shared.capacity import (
    CapacityOwnerKind,
    CapacityOwnerSource,
    CapacityPoolSizingState,
    CapacityPoolSizingStateUpdate,
)
from shared.compute_fleet import Pool
from shared.scheduling import SchedulerWorkerRequest
from tests.metric_helpers import metric_value
from tests.provider_fixtures import configure_test_provider


class _RealRedisActors(Protocol):
    def client(self) -> RedisClient: ...


_KUBERNETES_CAPACITY_OWNER_ID = "6fb19db5-ddd0-478d-8f4a-cdf422ad438c"


def test_static_worker_pool_drain_uses_authoritative_owner_policy() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    pool = _kubernetes_pool()
    scaler = _ReplicaScaler()
    states = _ReplicaStateStore()
    controller = StaticWorkerPoolDrainController(
        pool,
        _WorkerRepository(
            [
                _worker("worker-old", now - timedelta(seconds=120)),
                _worker("worker-newer", now - timedelta(seconds=90)),
            ]
        ),
        _ContainerRepository(),
        scaler,
        states,
        _SizingStateStore(pool),
    )

    result = controller.reconcile(now=now)

    assert result.action is WorkerPoolDrainAction.ScaleWorkerPool
    assert result.desired_replicas == 1
    assert result.drained_worker_ids == ["worker-old"]
    assert scaler.scale_calls == [(_KUBERNETES_CAPACITY_OWNER_ID, "gpu", 1)]
    assert states.values[_KUBERNETES_CAPACITY_OWNER_ID].desired_replicas == 1


def test_static_worker_pool_drain_requires_every_owned_worker_to_be_idle() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    pool = _kubernetes_pool()
    scaler = _ReplicaScaler()
    controller = StaticWorkerPoolDrainController(
        pool,
        _WorkerRepository(
            [
                _worker("worker-old", now - timedelta(seconds=120)),
                _worker("worker-active-window", now - timedelta(seconds=30)),
            ]
        ),
        _ContainerRepository(),
        scaler,
        _ReplicaStateStore(),
        _SizingStateStore(pool),
    )

    result = controller.reconcile(now=now)

    assert result.action is WorkerPoolDrainAction.None_
    assert result.reason == "pool has workers inside drain idle window"
    assert scaler.scale_calls == []


def test_worker_pool_drain_scales_idle_static_pool_down_once(
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    locks = RedisWorkerPoolStateRepository(redis)
    replica_states = RedisWorkerPoolReplicaStateStore(redis)
    scaler = _ReplicaScaler()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    pool = _kubernetes_pool()
    _add_worker(workers, "worker-old", now - timedelta(seconds=120))
    _add_worker(workers, "worker-newer", now - timedelta(seconds=90))
    reservations = _reservation_service(redis)
    service = WorkerPoolDrainService(
        redis,
        locks,
        lambda: static_worker_pool_drain_controllers(
            [pool],
            workers,
            containers,
            scaler,
            replica_states,
            _SizingStateStore(pool),
        ),
        reservations,
    )

    first = service.reconcile(now=now)
    second = service.reconcile(now=now)

    assert first[0].action is WorkerPoolDrainAction.ScaleWorkerPool
    assert first[0].desired_replicas == 1
    assert first[0].drained_worker_ids == ["worker-old"]
    assert first[0].capacity_owner_id == _KUBERNETES_CAPACITY_OWNER_ID
    assert scaler.scale_calls == [(_KUBERNETES_CAPACITY_OWNER_ID, "gpu", 1)]
    assert second[0].action is WorkerPoolDrainAction.None_
    assert second[0].reason == "waiting for desired worker replicas to register or terminate"
    assert scaler.scale_calls == [(_KUBERNETES_CAPACITY_OWNER_ID, "gpu", 1)]
    stored = replica_states.get_state(_KUBERNETES_CAPACITY_OWNER_ID)
    assert stored is not None
    assert stored.capacity_owner_id == _KUBERNETES_CAPACITY_OWNER_ID
    assert stored.desired_replicas == 1


def test_scheduler_records_worker_pool_drain_events_and_metrics(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    locks = RedisWorkerPoolStateRepository(redis)
    replica_states = RedisWorkerPoolReplicaStateStore(redis)
    scaler = _ReplicaScaler()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    pool = _kubernetes_pool()
    _add_worker(workers, "worker-old", now - timedelta(seconds=120))
    _add_worker(workers, "worker-newer", now - timedelta(seconds=90))
    scheduler = Scheduler(
        isolated_services,
        capacity=SchedulerCapacityControls(
            worker_pool_drain=WorkerPoolDrainService(
                redis,
                locks,
                lambda: static_worker_pool_drain_controllers(
                    [pool],
                    workers,
                    containers,
                    scaler,
                    replica_states,
                    _SizingStateStore(pool),
                ),
                _reservation_service(redis),
            )
        ),
        reconcile_agent_pools_enabled=False,
    )

    results = scheduler.drain_worker_pools(now=now)

    assert results[0].action is WorkerPoolDrainAction.ScaleWorkerPool
    event = next(
        item
        for item in isolated_services.events.list()
        if item.action == "worker_pool.drain.decision"
    )
    assert event.resource_type == "worker_pool"
    assert event.resource_id == "gpu"
    assert event.data["action"] == "scale-worker-pool"
    assert event.data["drained_worker_ids"] == ["worker-old"]
    snapshot = isolated_services.metrics.latest()
    labels = {"source": "worker_pool.drain", "pool_name": "gpu"}
    assert (
        metric_value(
            snapshot.counters,
            "worker_pool_drain_decisions_total",
            **labels,
            action="scale-worker-pool",
        )
        == 1
    )
    assert metric_value(snapshot.gauges, "worker_pool_desired_replicas", **labels) == 1
    assert metric_value(snapshot.gauges, "worker_pool_observed_replicas", **labels) == 1
    assert metric_value(snapshot.gauges, "worker_pool_drained_workers", **labels) == 1


def test_worker_pool_drain_holds_when_pool_has_active_containers(
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    locks = RedisWorkerPoolStateRepository(redis)
    replica_states = RedisWorkerPoolReplicaStateStore(redis)
    scaler = _ReplicaScaler()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    pool = _kubernetes_pool()
    _add_worker(workers, "worker-busy", now - timedelta(seconds=120))
    _add_worker(workers, "worker-idle", now - timedelta(seconds=120))
    containers.set_container_state(
        SchedulerContainerState(
            container_id="container-1",
            workspace_id="workspace",
            stub_id="stub",
            worker_id="worker-busy",
            status=SchedulerContainerStatus.Running,
        )
    )
    service = WorkerPoolDrainService(
        redis,
        locks,
        lambda: static_worker_pool_drain_controllers(
            [pool],
            workers,
            containers,
            scaler,
            replica_states,
            _SizingStateStore(pool),
        ),
        _reservation_service(redis),
    )

    result = service.reconcile(now=now)

    assert result[0].action is WorkerPoolDrainAction.None_
    assert result[0].reason == "pool has active containers"
    assert scaler.scale_calls == []


def test_worker_pool_drain_holds_while_capacity_allocation_is_open(
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    locks = RedisWorkerPoolStateRepository(redis)
    scaler = _ReplicaScaler()
    pool = _kubernetes_pool()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    _add_worker(workers, "worker-old", now - timedelta(seconds=120))
    _add_worker(workers, "worker-newer", now - timedelta(seconds=90))
    reservations = _reservation_service(redis)
    _reserve_allocation(
        reservations.reservations,
        capacity_owner_id=pool.capacity_owner_id,
        pool_name=pool.name,
        owner_kind=CapacityOwnerKind.GlobalKubernetesDeployment,
        container_id="container-awaiting-capacity",
        now=now,
    )
    service = WorkerPoolDrainService(
        redis,
        locks,
        lambda: static_worker_pool_drain_controllers(
            [pool],
            workers,
            containers,
            scaler,
            RedisWorkerPoolReplicaStateStore(redis),
            _SizingStateStore(pool),
        ),
        reservations,
    )

    result = service.reconcile(now=now)

    assert result[0].action is WorkerPoolDrainAction.None_
    assert result[0].reason == "capacity owner has open provisioning allocations"
    assert scaler.scale_calls == []


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


def _kubernetes_pool() -> Pool:
    return Pool(
        capacity_owner_id=_KUBERNETES_CAPACITY_OWNER_ID,
        capacity_owner_kind=CapacityOwnerKind.GlobalKubernetesDeployment,
        capacity_owner_source=CapacityOwnerSource.Kubernetes,
        name="gpu",
        provider="kubernetes",
        initial_workers=2,
        min_workers=1,
        max_workers=3,
        scaling_enabled=True,
        worker_cpu_millicores=1000,
        worker_memory_mib=1024,
        idle_drain_timeout_seconds=60,
    )


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
    pool_name: str = "gpu",
    machine_id: str = "",
    capacity_owner_id: str = _KUBERNETES_CAPACITY_OWNER_ID,
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


def _worker(
    worker_id: str,
    updated_at: datetime,
    *,
    capacity_owner_id: str = _KUBERNETES_CAPACITY_OWNER_ID,
) -> SchedulerWorkerRecord:
    return SchedulerWorkerRecord(
        worker_id=worker_id,
        pool_name="gpu",
        capacity_owner_id=capacity_owner_id,
        status=SchedulerWorkerStatus.Available,
        total_cpu_millicores=1000,
        total_memory_mib=1024,
        free_cpu_millicores=1000,
        free_memory_mib=1024,
        created_at=updated_at,
        updated_at=updated_at,
    )


class _WorkerRepository:
    def __init__(self, workers: list[SchedulerWorkerRecord]) -> None:
        self.workers = workers

    def list_workers_in_pool(self, pool_name: str) -> list[SchedulerWorkerRecord]:
        return [worker for worker in self.workers if worker.pool_name == pool_name]


class _ContainerRepository:
    def list_by_worker(self, worker_id: str) -> list[SchedulerContainerState]:
        _ = worker_id
        return []


class _ReplicaStateStore:
    def __init__(self) -> None:
        self.values: dict[str, WorkerPoolReplicaState] = {}

    def get_state(self, capacity_owner_id: str) -> WorkerPoolReplicaState | None:
        return self.values.get(capacity_owner_id)

    def save_state(self, state: WorkerPoolReplicaState) -> WorkerPoolReplicaState:
        self.values[state.capacity_owner_id] = state
        return state


class _SizingStateStore:
    def __init__(self, pool: Pool) -> None:
        self.state = CapacityPoolSizingState(
            capacity_owner_id=pool.capacity_owner_id,
            pool_name=pool.name,
            workspace_id="workspace",
            initial_target_reached=True,
        )

    def get_pool_sizing_state(self, capacity_owner_id: str) -> CapacityPoolSizingState:
        if capacity_owner_id != self.state.capacity_owner_id:
            raise ValueError("unknown capacity owner")
        return self.state

    def compare_and_set_pool_sizing_state(
        self,
        update: CapacityPoolSizingStateUpdate,
    ) -> CapacityPoolSizingState:
        if update.expected_revision != self.state.revision:
            raise ValueError("sizing revision changed")
        current = self.state
        self.state = CapacityPoolSizingState(
            capacity_owner_id=current.capacity_owner_id,
            pool_name=current.pool_name,
            workspace_id=current.workspace_id,
            revision=current.revision + 1,
            initial_target_reached=update.initial_target_reached,
            operation_id=update.operation_id,
            target_units=update.target_units,
            operation_started_at=update.operation_started_at,
            last_scale_up_at=update.last_scale_up_at,
            last_scale_down_at=update.last_scale_down_at,
            retry_after_at=update.retry_after_at,
            consecutive_failures=update.consecutive_failures,
            terminal_reason=update.terminal_reason,
        )
        return self.state


class _ReplicaScaler:
    def __init__(self) -> None:
        self.desired_replicas = 2
        self.observed_replicas = 2
        self.scale_calls: list[tuple[str, str, int]] = []

    def describe_worker_pool(
        self,
        pool: Pool,
        *,
        reservation_id: str,
        operation_id: str,
    ) -> WorkerPoolReplicaScaleResult:
        assert reservation_id == f"pool-drain-{pool.capacity_owner_id}"
        assert operation_id == reservation_id
        return WorkerPoolReplicaScaleResult(
            outcome=WorkerPoolReplicaScaleOutcome.ExistingPending,
            capacity_owner_id=pool.capacity_owner_id,
            pool_name=pool.name,
            desired_replicas=self.desired_replicas,
            observed_replicas=self.observed_replicas,
            provider="kubernetes",
            target=f"deployment/{pool.name}",
            reason="described authoritative worker pool",
        )

    def scale_worker_pool(
        self,
        pool: Pool,
        replicas: int,
        *,
        reservation_id: str,
        operation_id: str,
    ) -> WorkerPoolReplicaScaleResult:
        assert reservation_id == f"pool-drain-{pool.capacity_owner_id}"
        assert operation_id == reservation_id
        self.scale_calls.append((pool.capacity_owner_id, pool.name, replicas))
        self.desired_replicas = replicas
        self.observed_replicas = replicas
        return WorkerPoolReplicaScaleResult(
            outcome=WorkerPoolReplicaScaleOutcome.Requested,
            capacity_owner_id=pool.capacity_owner_id,
            pool_name=pool.name,
            desired_replicas=replicas,
            observed_replicas=replicas,
            provider="kubernetes",
            target=f"deployment/{pool.name}",
            reason="scaled authoritative worker pool",
        )
