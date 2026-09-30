from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import pytest
from compute.providers import (
    ProviderCapacityPhase,
    ProviderUnitInstance,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
)
from compute.state import ComputeUnitState, RedisComputeStateRepository
from database.context import ServiceContext
from database.repositories.compute import ComputeProviderInstanceRepository
from packages.compute.tests.pooled_fixtures import (
    _PooledProvider,
    _SchedulerHooks,
    _seed_connection,
    _seed_serving_machine,
    pooled_service,
    prepare_unit,
)
from scheduler.capacity_reservations import (
    CapacityReservationService,
    RedisCapacityReservationRepository,
)
from scheduler.compute_hooks import SchedulerComputeHooks
from scheduler.pool_drain import (
    WorkerPoolDrainAction,
    WorkerPoolDrainService,
    managed_compute_drain_controllers,
)
from scheduler.state import RedisSchedulerContainerRepository, RedisSchedulerWorkerRepository
from shared.compute_enrollment import agent_machine_worker_id
from shared.scheduling import (
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
    SchedulerWorkerStatus,
)
from shared.timestamps import utc_now
from tests.real_redis import RealRedisActors


@dataclass
class _TemplateProvider(_PooledProvider):
    template: str = "1"
    versions: dict[str, str] = field(default_factory=dict)
    retired: set[str] = field(default_factory=set)

    def _snapshot(
        self,
        request: ProviderUnitRequest,
        *,
        phase: ProviderCapacityPhase = ProviderCapacityPhase.Ready,
    ) -> ProviderUnitSnapshot:
        snapshot = super()._snapshot(request, phase=phase)
        active = set(self.versions) - self.retired
        for _ in range(max(0, self.desired - len(active))):
            self.versions[f"i-{len(self.versions):017x}"] = self.template
        instances = [
            ProviderUnitInstance(
                provider_instance_id=instance_id,
                status="active",
                booted_template_version=version,
            )
            for instance_id, version in self.versions.items()
            if instance_id not in self.retired
        ][: self.desired]
        return snapshot.model_copy(
            update={
                "instances": instances,
                "observed_machines": len(instances),
                "current_template_version": self.template,
            }
        )

    def release_machine(
        self, request: ProviderUnitRequest, provider_instance_id: str
    ) -> ProviderUnitSnapshot:
        self.retired.add(provider_instance_id)
        return super().release_machine(request, provider_instance_id)


@pytest.mark.parametrize(
    "floor,busy,expired", [(0, False, False), (1, False, False), (0, True, False), (1, False, True)]
)
def test_drain_preserves_warm_busy_and_foreign_capacity(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
    floor: int,
    busy: bool,
    expired: bool,
) -> None:
    _seed_connection(service_context)
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    states = RedisComputeStateRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    compute = pooled_service(
        service_context,
        _TemplateProvider(),
        scheduler_hooks=SchedulerComputeHooks(states, workers),
    )
    pool = prepare_unit(compute, desired=2)
    compute.reconciliation.reconcile_pooled_capacity()
    now = utc_now()
    machines = compute.machines.internal_unit_machine_by_instance(pool.workspace_id, pool.id)
    warm, burst = [machines[key] for key in sorted(machines)]
    foreign = str(uuid4())
    for machine_id, owner, age in (
        (warm, pool.id, 60),
        (burst, pool.id, 10),
        (foreign, str(uuid4()), 90),
    ):
        workers.add_worker(
            SchedulerWorkerRecord(
                worker_id=agent_machine_worker_id(machine_id),
                workspace_id=pool.workspace_id,
                placement=pool.placement,
                capacity_owner_id=owner,
                machine_id=machine_id,
                status=SchedulerWorkerStatus.Available,
                request_poll_expires_at=now
                if expired and machine_id == warm
                else now + timedelta(hours=1),
                created_at=now - timedelta(minutes=age),
                updated_at=now - timedelta(minutes=10),
            ),
            now=now - timedelta(minutes=10),
        )
        if busy and owner == pool.id:
            containers.set_container_state(
                SchedulerContainerState(
                    container_id=str(uuid4()),
                    stub_id=str(uuid4()),
                    workspace_id=pool.workspace_id,
                    worker_id=agent_machine_worker_id(machine_id),
                    status=SchedulerContainerStatus.Running,
                )
            )
    state = ComputeUnitState(
        workspace_id=pool.workspace_id,
        capacity_owner_id=pool.id,
        placement=pool.placement,
        name=pool.name,
        provider=pool.provider_ref,
        desired_machines=2,
        active_machines=2,
        min_machines=floor,
    )
    drain = WorkerPoolDrainService(
        lambda: managed_compute_drain_controllers(compute, [state], workers, containers),
        CapacityReservationService(RedisCapacityReservationRepository(redis), lambda: []),
    )

    [result] = drain.reconcile(now=now)
    assert result.error == ""
    durable = compute.providers.get_internal_unit(pool.workspace_id, pool.id)
    assert durable.desired_machines == (2 if busy else 1), result
    assert result.action is (
        WorkerPoolDrainAction.None_ if busy else WorkerPoolDrainAction.TerminateProviderMachine
    )
    if not busy:
        assert result.machine_id == (warm if expired else burst)
        with service_context.database.session() as session:
            retired = ComputeProviderInstanceRepository(session).get_by_machine(result.machine_id)
        assert retired is not None and retired.status == "terminating"
        assert retired.terminating_reason == "idle_pool_scale_down"
        if floor:
            state.active_machines = 1
            [held] = drain.reconcile(now=now + timedelta(minutes=2))
            assert held.error == ""
            assert held.action is WorkerPoolDrainAction.None_
            assert (
                compute.providers.get_internal_unit(pool.workspace_id, pool.id).desired_machines
                == 1
            )
    assert workers.get_worker(agent_machine_worker_id(foreign)) is not None


@pytest.mark.parametrize("busy", [False, True])
def test_template_replacement_keeps_capacity_and_running_work(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
    busy: bool,
) -> None:
    _seed_connection(service_context)
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    states = RedisComputeStateRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    provider = _TemplateProvider()
    compute = pooled_service(
        service_context, provider, scheduler_hooks=SchedulerComputeHooks(states, workers)
    )
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_pooled_capacity()
    now = utc_now()

    def register(instance_id: str) -> str:
        machine_id = str(uuid4())
        _seed_serving_machine(
            service_context,
            pool,
            _SchedulerHooks(),
            machine_id=machine_id,
            instance_id=instance_id,
            now=now,
        )
        workers.add_worker(
            SchedulerWorkerRecord(
                worker_id=agent_machine_worker_id(machine_id),
                workspace_id=pool.workspace_id,
                capacity_owner_id=pool.id,
                placement=pool.placement,
                machine_id=machine_id,
                status=SchedulerWorkerStatus.Available,
                request_poll_expires_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            ),
            now=now,
        )
        return machine_id

    old = register("i-00000000000000000")
    state = ComputeUnitState(
        workspace_id=pool.workspace_id,
        capacity_owner_id=pool.id,
        placement=pool.placement,
        name=pool.name,
        provider=pool.provider_ref,
        desired_machines=1,
        active_machines=1,
        min_machines=1,
    )
    drain = WorkerPoolDrainService(
        lambda: managed_compute_drain_controllers(compute, [state], workers, containers),
        CapacityReservationService(RedisCapacityReservationRepository(redis), lambda: []),
    )
    provider.template = "2"
    [surge] = drain.reconcile(now=now)
    assert surge.error == ""
    assert surge.action is WorkerPoolDrainAction.SurgeReplacementMachine
    assert compute.providers.get_internal_unit(pool.workspace_id, pool.id).desired_machines == 1
    assert len(compute.machines.internal_unit_machine_by_instance(pool.workspace_id, pool.id)) == 2
    [waiting] = drain.reconcile(now=now)
    assert waiting.error == ""
    assert waiting.action is WorkerPoolDrainAction.None_
    if busy:
        provider.template = "3"
    register("i-00000000000000001")
    state.active_machines = 2
    if busy:
        containers.set_container_state(
            SchedulerContainerState(
                container_id=str(uuid4()),
                stub_id=str(uuid4()),
                workspace_id=pool.workspace_id,
                worker_id=agent_machine_worker_id(old),
                status=SchedulerContainerStatus.Running,
            )
        )
    [draining] = drain.reconcile(now=now)
    assert draining.error == ""
    assert draining.action is WorkerPoolDrainAction.DrainSupersededMachine
    [settled] = drain.reconcile(now=now)
    assert settled.error == ""
    assert settled.action is (
        WorkerPoolDrainAction.None_ if busy else WorkerPoolDrainAction.TerminateProviderMachine
    )
    assert compute.providers.get_internal_unit(pool.workspace_id, pool.id).desired_machines == 1
    with service_context.database.session() as session:
        old_record = ComputeProviderInstanceRepository(session).get_by_machine(old)
    assert old_record is not None
    assert (old_record.status == "terminating") is not busy
