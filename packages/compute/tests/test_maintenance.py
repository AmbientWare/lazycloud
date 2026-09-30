from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from compute.fleet_policy import FleetReserveSnapshot, ReserveConditions, plan_market_reserve
from compute.providers import ProviderUnitRequest, ProviderUnitSnapshot
from compute.release_status import ComputeReleaseStatusService
from compute.reserve_state import RedisFleetReserveState
from compute.state import RedisComputeStateRepository
from database.context import ServiceContext
from database.repositories.capacity_maintenance import CapacityMaintenanceRepository
from database.repositories.compute import (
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.orchestration import MachineRepository
from database.repositories.worker_releases import WorkerReleaseRepository
from packages.compute.tests.pooled_fixtures import (
    _allow_scale,
    _PooledProvider,
    _SchedulerHooks,
    _seed_connection,
    _seed_serving_machine,
    pooled_service,
    prepare_unit,
)
from scheduler.compute_hooks import SchedulerComputeHooks
from scheduler.state import RedisSchedulerWorkerRepository
from shared.capacity_maintenance import CapacityMaintenancePhase
from shared.compute_enrollment import AgentCapacityState, agent_machine_worker_id
from shared.compute_fleet import Machine
from shared.compute_policy import ComputeUnitPhase
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.releases import ActiveRelease, AgentArtifact, ReleaseMachinePhase, ReleaseTarget
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from tests.real_redis import RealRedisActors


def test_named_retirement_ignores_absent_machines_and_preserves_retry_intent(
    service_context: ServiceContext,
) -> None:
    class UnavailableRetirement(_PooledProvider):
        def release_machine(
            self, request: ProviderUnitRequest, provider_instance_id: str
        ) -> ProviderUnitSnapshot:
            raise RuntimeError("provider unavailable")

    _seed_connection(service_context)
    provider = UnavailableRetirement()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=2)
    compute.reconciliation.reconcile_pooled_capacity()
    machine_id = str(uuid4())
    with service_context.database.session() as session:
        MachineRepository(session).upsert(
            Machine(id=machine_id, capacity_owner_id=pool.capacity_owner_id),
            workspace_id=pool.workspace_id,
        )
        instances = ComputeProviderInstanceRepository(session)
        record = instances.list_for_pool(pool.id)[0]
        instances.upsert(record.model_copy(update={"machine_id": machine_id}))
        absent_id = str(uuid4())
        instances.upsert(
            record.model_copy(
                update={
                    "id": absent_id,
                    "instance_id": "i-fffffffffffffffff",
                    "machine_id": None,
                    "missing_since": datetime.now(UTC),
                }
            )
        )

    for _attempt in range(2):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            compute.reserve_machines.release_internal_unit_machine(
                pool.workspace_id, pool.capacity_owner_id, machine_id
            )
        durable, _snapshot = compute.scaling.describe_internal_unit(
            pool.workspace_id, pool.capacity_owner_id
        )
        assert durable.desired_machines == 1
        with service_context.database.session() as session:
            retired = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
        assert retired is not None
        assert retired.status == "terminating"
        assert retired.terminating_reason == "idle_pool_scale_down"
        with service_context.database.session() as session:
            absent = ComputeProviderInstanceRepository(session).get(absent_id)
        assert absent is not None
        assert absent.status == "active"


def test_retiring_machine_cannot_acquire_another_planned_replacement(
    service_context: ServiceContext,
) -> None:
    class LingeringRetirement(_PooledProvider):
        def release_machine(
            self, request: ProviderUnitRequest, provider_instance_id: str
        ) -> ProviderUnitSnapshot:
            return self._snapshot(request)

    _seed_connection(service_context)
    provider = LingeringRetirement()
    hooks = _SchedulerHooks()
    compute = pooled_service(service_context, provider, scheduler_hooks=hooks)
    pool = prepare_unit(compute, desired=2)
    compute.reconciliation.reconcile_pooled_capacity()
    now = datetime.now(UTC)
    machine_ids = [str(uuid4()), str(uuid4())]
    for index, machine_id in enumerate(machine_ids):
        _seed_serving_machine(
            service_context,
            pool,
            hooks,
            machine_id=machine_id,
            instance_id=f"i-{index:017x}",
            now=now,
        )
        with service_context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            enrollment = enrollments.by_machine(pool.workspace_id, machine_id)
            assert enrollment is not None
            enrollments.save(
                enrollment.model_copy(
                    update={
                        "capacity_state": AgentCapacityState.Cordoned,
                        "capacity_notice_at": now + timedelta(seconds=120),
                    }
                )
            )
    old_machine, other_machine = machine_ids
    compute.maintenance.begin_internal_unit_replacement(
        pool.workspace_id, pool.id, old_machine, template_version="next"
    )
    compute.scaling.scale_internal_unit(pool.workspace_id, pool.id, 2, before_mutation=_allow_scale)
    retired = compute.reserve_machines.release_internal_unit_machine(
        pool.workspace_id, pool.id, old_machine
    )
    assert retired.desired_machines == 2
    assert retired.replacement_machine_id == ""
    with service_context.database.session() as session:
        record = ComputeProviderInstanceRepository(session).get_by_machine(old_machine)
    assert record is not None and record.status == "terminating"
    _durable, snapshot = compute.scaling.describe_internal_unit(pool.workspace_id, pool.id)
    assert record.instance_id in {item.provider_instance_id for item in snapshot.instances}
    assert compute.maintenance.internal_unit_replaceable_machines(pool.workspace_id, pool.id) == {
        other_machine
    }

    with pytest.raises(ConflictError):
        compute.maintenance.begin_internal_unit_replacement(
            pool.workspace_id, pool.id, old_machine, template_version="next"
        )
    assert compute.providers.get_internal_unit(pool.workspace_id, pool.id) == retired
    replacement = compute.maintenance.begin_internal_unit_replacement(
        pool.workspace_id, pool.id, other_machine, template_version="next"
    )
    assert replacement.replacement_machine_id == other_machine
    assert replacement.desired_machines == 2


@pytest.mark.parametrize("platform_fleet", [True, False])
def test_release_rollout_preserves_singleton_until_replacement_and_fresh_intake(
    service_context: ServiceContext,
    platform_fleet: bool,
    real_redis_actors: RealRedisActors,
) -> None:
    _seed_connection(service_context, platform_fleet=platform_fleet)
    provider = _PooledProvider()
    hooks = _SchedulerHooks()
    compute = pooled_service(
        service_context,
        provider,
        scheduler_hooks=hooks,
        reserve_state=RedisFleetReserveState(real_redis_actors.client()),
    )
    assert compute.providers.reserve_state is not None
    compute.providers.reserve_state.publish(
        plan_market_reserve(
            compute.providers.fleet_policy,
            FleetReserveSnapshot((), (), 0, 0, 0),
            ReserveConditions(now=datetime.now(UTC)),
        ),
        generated_at=datetime.now(UTC),
        release=None,
    )
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_unit_capacity(pool.id)
    now = datetime.now(UTC)
    machine_id = str(uuid4())
    _seed_serving_machine(
        service_context,
        pool,
        hooks,
        machine_id=machine_id,
        instance_id="i-00000000000000000",
        now=now,
    )
    source = SchedulerWorkerRecord(
        worker_id=agent_machine_worker_id(machine_id),
        machine_id=machine_id,
        workspace_id=pool.workspace_id,
        capacity_owner_id=pool.capacity_owner_id,
        placement=pool.placement,
        runtime_image="worker:old",
        agent_binary_sha256="a" * 64,
        admitted_release_generation=1,
        status=SchedulerWorkerStatus.Available,
        request_poll_expires_at=now + timedelta(minutes=5),
        availability_zone="use1-az1",
        total_disk_volumes=2,
        free_disk_volumes=1,
        total_cpu_millicores=4_000,
        free_cpu_millicores=2_000,
    )
    release = ActiveRelease(
        generation=2,
        manifest_url="https://example.test/release",
        target=ReleaseTarget(
            version="2",
            source_revision="b" * 40,
            worker_image="worker:next",
            agent=AgentArtifact(url="https://example.test/agent", sha256="b" * 64, size_bytes=1),
        ),
    )
    if platform_fleet:
        idle = source.model_copy(update={"free_cpu_millicores": 4000, "free_disk_volumes": 2})
        planned = compute.maintenance.prepare_worker_release(idle, release, [idle])
        assert planned.surge_machines == 0
    with (
        pytest.raises(ConflictError, match="reserved replacement"),
        compute.maintenance.worker_release_admission(source, release, [source]),
    ):
        pass
    rollout = compute.rollouts
    rollout.reconcile(release, [source], now=now)
    rollout.reconcile(release, [source], now=now)
    paired = compute.providers.get_internal_unit(pool.workspace_id, pool.id)
    assert paired.desired_machines == 1
    assert paired.maintenance_surge_machines == 1
    with service_context.database.session() as session:
        [operation] = CapacityMaintenanceRepository(session).active_for_pools([pool.id])
    assert operation.source_machine_id == source.machine_id
    assert operation.release_generation == 2
    assert paired.observed_machines == 2
    status = ComputeReleaseStatusService(service_context.database).read(release, [source])
    assert not status.complete
    assert status.pending_capacity_owners == [pool.id]
    replacement = source.model_copy(
        update={
            "worker_id": str(uuid4()),
            "machine_id": str(uuid4()),
            "runtime_image": "worker:next",
            "agent_binary_sha256": "b" * 64,
            "free_cpu_millicores": 4_000,
        }
    )
    for refused in (
        replacement.model_copy(update={"capacity_owner_id": str(uuid4())}),
        replacement.model_copy(update={"availability_zone": "use1-az2"}),
        replacement.model_copy(update={"free_cpu_millicores": 1_000}),
        replacement.model_copy(update={"request_poll_expires_at": now - timedelta(seconds=1)}),
    ):
        with (
            pytest.raises(ConflictError, match="reserved replacement"),
            compute.maintenance.worker_release_admission(source, release, [source, refused]),
        ):
            pass
    with compute.maintenance.worker_release_admission(source, release, [source, replacement]):
        pass
    with service_context.database.session() as session:
        WorkerReleaseRepository(session).record_update_error(
            source.worker_id,
            source.machine_id,
            generation=release.generation,
            reason="the agent service has no update supervisor",
        )
    blocked = ComputeReleaseStatusService(service_context.database).read(release, [source])
    blocked_source = next(item for item in blocked.machines if item.machine_id == machine_id)
    assert blocked_source.phase is ReleaseMachinePhase.Blocked
    assert not blocked_source.accepting_work
    previous_release = release
    release = release.model_copy(
        update={
            "generation": 3,
            "target": release.target.model_copy(
                update={
                    "worker_image": source.runtime_image,
                    "agent": AgentArtifact(
                        url="https://example.test/agent",
                        sha256=source.agent_binary_sha256,
                        size_bytes=1,
                    ),
                }
            ),
        }
    )
    draining = source.model_copy(update={"status": SchedulerWorkerStatus.Draining})
    rollout.reconcile(release, [draining, replacement], now=now)
    rollout.reconcile(previous_release, [draining, replacement], now=now)
    with service_context.database.session() as session:
        [operation] = CapacityMaintenanceRepository(session).active_for_pools([pool.id])
    assert operation.release_generation == 3
    replacement = replacement.model_copy(
        update={
            "runtime_image": release.target.worker_image,
            "agent_binary_sha256": source.agent_binary_sha256,
        }
    )
    with compute.maintenance.worker_release_admission(draining, release, [draining, replacement]):
        pass
    with (
        pytest.raises(ConflictError, match="newer release"),
        compute.maintenance.worker_release_admission(
            draining, previous_release, [draining, replacement]
        ),
    ):
        pass
    current = source.model_copy(
        update={
            "request_poll_expires_at": None,
        }
    )
    with service_context.database.session() as session:
        assert not WorkerReleaseRepository(session).complete_update(current)
    rollout.reconcile(release, [current, replacement], now=now)
    assert (
        compute.providers.get_internal_unit(pool.workspace_id, pool.id).maintenance_surge_machines
        == 1
    )
    current.request_poll_expires_at = now + timedelta(minutes=5)
    with service_context.database.session() as session:
        assert WorkerReleaseRepository(session).complete_update(current)
    rollout.reconcile(release, [current, replacement], now=now)
    settled = compute.providers.get_internal_unit(pool.workspace_id, pool.id)
    assert settled.replacement_machine_id == ""
    assert settled.maintenance_surge_machines == 0
    assert settled.desired_machines == 1
    status = ComputeReleaseStatusService(service_context.database).read(release, [current])
    assert pool.id in status.pending_capacity_owners
    rollout.reconcile(release, [current, replacement], now=now)
    with service_context.database.session() as session:
        [retiring] = CapacityMaintenanceRepository(session).active_for_pools([pool.id])
        assert retiring.phase is CapacityMaintenancePhase.Retiring
    assert next(item for item in status.machines if item.machine_id == machine_id).current

    provider.delete_failure = RuntimeError("provider deletion unavailable")
    with pytest.raises(UpstreamUnavailableError, match="provider deletion unavailable"):
        compute.removal.delete_unit(pool.capacity_owner_id, workspace=pool.workspace_id)
    rollout.reconcile(release, [current, replacement], now=now)
    with service_context.database.session() as session:
        assert CapacityMaintenanceRepository(session).get(retiring.id) == retiring
        commitments = CapacityMaintenanceRepository(session).commitments([pool.id])
        assert commitments.operations == 1
        assert commitments.surge_machines == 1

    provider.delete_failure = None
    provider.lingering_storage.add("i-00000000000000000")
    rollout.reconcile(release, [], now=now)
    with service_context.database.session() as session:
        unit = ComputeUnitRepository(session).get(pool.id)
        assert unit is not None and unit.phase is ComputeUnitPhase.Deleting
        assert CapacityMaintenanceRepository(session).commitments([pool.id]) == commitments

    provider.lingering_storage.clear()
    rollout.reconcile(release, [], now=now)
    with service_context.database.session() as session:
        unit = ComputeUnitRepository(session).get(pool.id)
        assert unit is not None and unit.phase is ComputeUnitPhase.Deleted
        assert CapacityMaintenanceRepository(session).commitments([pool.id]) == commitments

    rollout.reconcile(release, [], now=now)
    with service_context.database.session() as session:
        repository = CapacityMaintenanceRepository(session)
        completed = repository.get(retiring.id)
        assert completed is not None and completed.phase is CapacityMaintenancePhase.Complete
        assert repository.commitments([pool.id]).operations == 0
        if platform_fleet:
            assert not ComputeUnitRepository(session).platform_reserve_rows().units


def test_worker_update_preserves_its_fence_without_blocking_another_pool(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
) -> None:
    _seed_connection(service_context, platform_fleet=True)
    provider = _PooledProvider()
    hooks = _SchedulerHooks()
    compute = pooled_service(service_context, provider, scheduler_hooks=hooks)
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_unit_capacity(pool.id)
    now = datetime.now(UTC)
    machine_id = str(uuid4())
    _seed_serving_machine(
        service_context,
        pool,
        hooks,
        machine_id=machine_id,
        instance_id="i-00000000000000000",
        now=now,
    )
    sibling_id = str(uuid4())
    with service_context.database.session() as session:
        sibling = ComputeUnitRepository(session).upsert(
            pool.model_copy(
                update={
                    "id": sibling_id,
                    "capacity_owner_id": sibling_id,
                    "name": "internal-sibling",
                    "selector": "internal-sibling",
                    "capability_key": f"{pool.capability_key}:sibling",
                    "desired_machines": 1,
                    "observed_machines": 1,
                }
            )
        )
        instances = ComputeProviderInstanceRepository(session)
        original = instances.list_for_pool(pool.id)[0]
        instances.upsert(
            original.model_copy(
                update={
                    "id": str(uuid4()),
                    "pool_id": sibling_id,
                    "instance_id": "i-00000000000000001",
                    "machine_id": None,
                }
            )
        )
    other_machine_id = str(uuid4())
    _seed_serving_machine(
        service_context,
        sibling,
        hooks,
        machine_id=other_machine_id,
        instance_id="i-00000000000000001",
        now=now,
    )
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    compute = pooled_service(
        service_context,
        provider,
        scheduler_hooks=SchedulerComputeHooks(RedisComputeStateRepository(redis), workers),
    )
    worker = workers.add_worker(
        SchedulerWorkerRecord(
            worker_id=agent_machine_worker_id(machine_id),
            machine_id=machine_id,
            capacity_owner_id=pool.capacity_owner_id,
            placement=pool.placement,
            runtime_image="worker:v1",
            agent_binary_sha256="a" * 64,
            status=SchedulerWorkerStatus.Available,
            request_poll_expires_at=now + timedelta(minutes=1),
        )
    )
    release = ActiveRelease(
        generation=2,
        manifest_url="https://artifacts.lazycloud.test/releases/v2.json",
        target=ReleaseTarget(
            version="2",
            source_revision="revision2",
            worker_image="worker:v2",
            agent=AgentArtifact(
                url="https://artifacts.lazycloud.test/agent/v2",
                sha256="b" * 64,
                size_bytes=1,
            ),
        ),
    )
    with compute.maintenance.worker_maintenance_admission(
        pool.workspace_id, pool.id, machine_id
    ) as session:
        WorkerReleaseRepository(session).begin_update(worker.worker_id, machine_id, release)
    workers.remove_worker(worker.worker_id)
    assert workers.get_worker(worker.worker_id) is None
    with compute.maintenance.worker_maintenance_admission(
        pool.workspace_id, sibling_id, other_machine_id
    ):
        pass
    updated = worker.model_copy(
        update={"runtime_image": "worker:v2", "agent_binary_sha256": "b" * 64}
    )
    with service_context.database.session() as session:
        releases = WorkerReleaseRepository(session)
        assert not releases.complete_update(worker)
        assert not releases.complete_update(
            updated.model_copy(update={"request_poll_expires_at": None})
        )
        assert releases.machine_has_update(machine_id)
    available = workers.add_worker(updated)
    with service_context.database.session() as session:
        assert WorkerReleaseRepository(session).complete_update(available)
    with compute.maintenance.worker_maintenance_admission(
        pool.workspace_id, sibling_id, other_machine_id
    ):
        pass
