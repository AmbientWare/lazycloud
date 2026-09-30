from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from compute.capacity_recovery import record_capacity_risk
from compute.providers import (
    ProviderOfferEligibility,
    ResolvedComputeProvider,
    ResolvedProviderPolicy,
)
from compute.reclaim import ComputeReclaimPolicy
from compute.service import ComputeServices
from database.context import ServiceContext
from database.repositories.capacity_recovery import CapacityRecoveryRepository
from database.repositories.compute import (
    ComputeCapacityOperationRepository,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.tables.capacity_recovery import CapacityRecoveryTable
from database.tables.compute import ComputeCapacityOperationTable
from identity.platform import PlatformNamespaceService
from packages.compute.tests.pooled_fixtures import (
    _allow_scale,
    _bootstrap,
    _machine_of,
    _mark_open_record_booting,
    _offer,
    _open_record,
    _PooledProvider,
    _SchedulerHooks,
    _seed_connection,
    _seed_serving_machine,
    pooled_service,
    prepare_unit,
)
from provider_aws.managed_pool import Boto3AwsManagedPoolClientProvider
from provider_clients.workspace_compute import WorkspaceComputeProviderResolver
from scheduler.capacity_reservations import RedisCapacityReservationRepository
from shared.capacity import CapacityFailureCode, CapacityOperationStatus
from shared.compute_enrollment import AgentCapacityState, MachineBootstrapFailureReason
from shared.compute_fleet import MachineLifecycle
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeResourceRequirements,
    ComputeUnitPhase,
    ComputeUnitRecord,
)
from shared.placement import Placement
from shared.supplier_costs import SupplierCostTerms
from sqlalchemy import func, select
from tests.real_redis import RealRedisActors


@pytest.mark.parametrize(
    "replacement_markets,failure_code",
    [
        (1, None),
        (2, None),
        (2, CapacityFailureCode.CapacityUnavailable),
        (2, CapacityFailureCode.ProviderQuotaExceeded),
    ],
)
def test_two_interrupted_workers_admit_distinct_replacements_once(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
    replacement_markets: int,
    failure_code: CapacityFailureCode | None,
) -> None:
    reject_first = failure_code is not None
    with service_context.database.session() as session:
        workspace_id = PlatformNamespaceService(service_context.database).get().id
    providers: list[ResolvedComputeProvider] = []
    capacity_providers: list[_PooledProvider] = []
    for index in range(replacement_markets + 1):
        offer = _offer().model_copy(
            update={
                "provider": f"aws:recovery-{index}",
                "id": f"recovery-{index}",
                "capability_key": f"recovery-{index}",
                "availability_zone": f"use1-az{index + 1}",
                "preemptible": True,
                "cost_terms": SupplierCostTerms(
                    compute_hourly_micros=100_000,
                    root_disk_hourly_micros=0,
                    public_ipv4_hourly_micros=0,
                ),
            }
        )
        capacity_provider = _PooledProvider(offer=offer)
        capacity_providers.append(capacity_provider)
        providers.append(
            ResolvedComputeProvider(
                ref=offer.provider,
                capacity_mode=ComputeCapacityMode.Pooled,
                pooled=capacity_provider,
                policy=ResolvedProviderPolicy(
                    workspace_id=workspace_id,
                    placement=Placement.platform(),
                    platform_fleet=True,
                    default_region=offer.region,
                    allowed_regions=(offer.region,),
                    allowed_offers=(
                        ProviderOfferEligibility(
                            region=offer.region,
                            instance_type=offer.instance_type,
                            preemptible=True,
                        ),
                    ),
                ),
            )
        )
    resolver = WorkspaceComputeProviderResolver(
        connections=lambda _workspace: (),
        capacity_workspace=lambda _connection: workspace_id,
        binaries_by_region={},
        client_provider=Boto3AwsManagedPoolClientProvider.from_default_chain(),
        platform_providers=lambda: tuple(providers),
    )
    hooks = _SchedulerHooks()
    compute = ComputeServices.create(
        service_context,
        provider_resolver=resolver,
        scheduler_hooks=hooks,
        capacity_owner_mutations=RedisCapacityReservationRepository(real_redis_actors.client()),
        pool_bootstrap_factory=_bootstrap,
    )
    source = compute.provisioning.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(preemptible=True),
        region="us-east-1",
        desired_machines=2,
        root_volume_gib=200,
        provider_ref=providers[0].ref,
    )
    compute.reconciliation.reconcile_unit_capacity(source.id)
    now = datetime.now(UTC)
    for index in range(2):
        machine_id = str(uuid4())
        _seed_serving_machine(
            service_context,
            source,
            hooks,
            machine_id=machine_id,
            instance_id=f"i-{index:017x}",
            now=now,
        )
        with service_context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            enrollment = enrollments.by_machine(workspace_id, machine_id)
            assert enrollment is not None
            interrupted = enrollments.save(
                enrollment.model_copy(
                    update={
                        "capacity_state": AgentCapacityState.Draining,
                        "capacity_observed_at": now,
                        "capacity_notice_at": now + timedelta(minutes=2),
                        "schedulable": False,
                    }
                )
            )
            record_capacity_risk(session, interrupted)
            record_capacity_risk(session, interrupted)
    compute.recovery.reconcile(now=now + timedelta(seconds=1))
    with service_context.database.session() as session:
        rows = session.scalars(select(CapacityRecoveryTable)).all()
        assert len(rows) == 2
        assert len({row.target_unit_id for row in rows}) == replacement_markets, [
            row.reason for row in rows
        ]
        assert all(row.source_adjusted and row.operation_id is not None for row in rows)
        operations = [
            ComputeCapacityOperationRepository(session).get(row.target_unit_id, row.operation_id)
            for row in rows
            if row.target_unit_id and row.operation_id
        ]
        assert len(operations) == 2
        assert all(operation is not None and operation.owns_capacity for operation in operations)
        assert len(CapacityRecoveryRepository(session).protected_sources(source.id)) == 2
    compute = ComputeServices.create(
        service_context,
        provider_resolver=resolver,
        scheduler_hooks=hooks,
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=RedisCapacityReservationRepository(real_redis_actors.client()),
    )
    if reject_first:
        capacity_providers[1].max_observed_machines = 0
        capacity_providers[1].last_capacity_failure_at = now + timedelta(seconds=6)
        capacity_providers[1].last_capacity_failure_code = failure_code
    compute.recovery.reconcile(now=now + timedelta(seconds=7))
    if reject_first:
        with service_context.database.session() as session:
            rows = session.scalars(select(CapacityRecoveryTable)).all()
            assert sum(row.target_unit_id is None for row in rows) == 1
            rejected_operation = next(
                operation
                for operation in operations
                if operation is not None
                and operation.capacity_owner_id
                == compute.providers.pooled_offer_owner_id(
                    providers[1], capacity_providers[1].offer
                )
            )
            released = ComputeCapacityOperationRepository(session).get(
                rejected_operation.capacity_owner_id, rejected_operation.operation_id
            )
            assert released is not None and released.status is CapacityOperationStatus.Released
            assert not released.owns_capacity
        compute.recovery.reconcile(now=now + timedelta(seconds=13))
        if failure_code is CapacityFailureCode.ProviderQuotaExceeded:
            with service_context.database.session() as session:
                rows = session.scalars(select(CapacityRecoveryTable)).all()
                failed = [row for row in rows if row.completed_at is not None]
                assert len(failed) == 1
                assert failed[0].reason == "provider compute quota exceeded"
                assert failed[0].attempt == 1 and failed[0].target_unit_id is None
                assert (
                    session.scalar(select(func.count()).select_from(ComputeCapacityOperationTable))
                    == 2
                )
            return
    with service_context.database.session() as session:
        rows = session.scalars(select(CapacityRecoveryTable)).all()
        assert sorted(row.attempt for row in rows) == ([1, 2] if reject_first else [1, 1])
        assert (
            sum(
                unit.desired_machines
                for unit in ComputeUnitRepository(session).list_platform_internal()
            )
            == 2
        )
        targets = [
            ComputeUnitRepository(session).get(row.target_unit_id)
            for row in rows
            if row.target_unit_id is not None
        ]
    replacement_ids: set[str] = set()
    target_instances: dict[str, int] = {}
    for target in targets:
        assert target is not None
        machine_id = str(uuid4())
        replacement_ids.add(machine_id)
        instance_index = target_instances.get(target.id, 0)
        target_instances[target.id] = instance_index + 1
        _seed_serving_machine(
            service_context,
            target,
            hooks,
            machine_id=machine_id,
            instance_id=f"i-{instance_index:017x}",
            now=now + timedelta(seconds=14),
        )
    compute.recovery.reconcile(now=now + timedelta(seconds=19))
    with service_context.database.session() as session:
        rows = session.scalars(select(CapacityRecoveryTable)).all()
        assert {row.replacement_machine_id for row in rows} == replacement_ids
        assert all(row.completed_at is None for row in rows)
        assert len(CapacityRecoveryRepository(session).protected_sources(source.id)) == 2
    compute.recovery.reconcile(now=now + timedelta(seconds=25))
    with service_context.database.session() as session:
        rows = session.scalars(select(CapacityRecoveryTable)).all()
        assert all(row.completed_at is not None for row in rows)
        assert CapacityRecoveryRepository(session).protected_sources(source.id) == set()


def test_internal_pool_bootstrap_phase_deadline_reclaims_only_after_it_elapses(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    hooks = _SchedulerHooks(intake_observing_since=datetime.now(UTC) - timedelta(hours=1))
    compute = pooled_service(service_context, provider, scheduler_hooks=hooks)
    pool = prepare_unit(compute, desired=1)
    started_at = datetime.now(UTC)
    compute.reconciliation.reconcile_pooled_capacity(now=started_at)
    booting = _mark_open_record_booting(service_context, pool.id, at=started_at)
    assert booting.machine_id is not None

    compute.reconciliation.reconcile_pooled_capacity(now=started_at + timedelta(seconds=200))
    with service_context.database.session() as session:
        [waiting] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert provider.release_calls == []
    assert waiting.status not in {"terminating", "deleted", "failed"}
    waiting_machine = _machine_of(service_context, waiting)
    assert waiting_machine.lifecycle is MachineLifecycle.Booting
    assert waiting_machine.lifecycle_at == started_at

    compute.reconciliation.reconcile_pooled_capacity(now=started_at + timedelta(seconds=301))
    with service_context.database.session() as session:
        [replacement] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        current = ComputeUnitRepository(session).get(pool.id)
    assert provider.release_calls == [booting.instance_id]
    assert replacement.launch_attempt == 2
    replacement_machine = _machine_of(service_context, replacement)
    assert replacement_machine.lifecycle is MachineLifecycle.Provisioning
    assert replacement_machine.lifecycle_failure is None
    assert replacement.machine_id != booting.machine_id
    assert replacement.terminating_reason == ""
    assert current is not None
    assert current.provider_state.degraded_reason is None
    assert current.desired_machines == 1
    assert provider.desired == 1


def test_repeated_bootstrap_failures_do_not_extend_the_reclaim_deadline(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    started_at = datetime.now(UTC)
    compute = pooled_service(
        service_context,
        provider,
        scheduler_hooks=_SchedulerHooks(intake_observing_since=started_at - timedelta(hours=1)),
    )
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_pooled_capacity(now=started_at)
    first_failure = compute.machines.record_provider_node_lifecycle(
        pool_id=pool.id,
        provider_instance_id="i-00000000000000000",
        lifecycle=MachineLifecycle.Failed,
        failure_reason=MachineBootstrapFailureReason.WorkerReadinessFailed,
        now=started_at + timedelta(seconds=10),
    )
    assert first_failure.lifecycle_at == started_at + timedelta(seconds=10)
    for elapsed in (299, 310):
        observed = compute.machines.record_provider_node_lifecycle(
            pool_id=pool.id,
            provider_instance_id="i-00000000000000000",
            lifecycle=MachineLifecycle.Failed,
            failure_reason=MachineBootstrapFailureReason.WorkerReadinessFailed,
            now=started_at + timedelta(seconds=elapsed),
        )
        assert observed.lifecycle_at == started_at + timedelta(seconds=10)

    compute.reconciliation.reconcile_pooled_capacity(now=started_at + timedelta(seconds=311))

    assert provider.release_calls == ["i-00000000000000000"]
    with service_context.database.session() as session:
        [replacement] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert replacement.launch_attempt == 2
    replacement_machine = _machine_of(service_context, replacement)
    assert replacement_machine.lifecycle is MachineLifecycle.Provisioning
    assert replacement_machine.lifecycle_at == started_at + timedelta(seconds=311)


def test_relaunch_exhaustion_durably_degrades_pool_until_explicit_capacity_mutation(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(
        service_context,
        provider,
        reclaim=ComputeReclaimPolicy(max_launch_attempts=2),
        scheduler_hooks=_SchedulerHooks(
            intake_observing_since=datetime.now(UTC) - timedelta(hours=1)
        ),
    )
    pool = prepare_unit(compute, desired=1)
    moment = datetime.now(UTC)
    compute.reconciliation.reconcile_pooled_capacity(now=moment)
    first = _mark_open_record_booting(service_context, pool.id, at=moment)
    assert first.launch_attempt == 1

    # Two looks per reclaim: the deadline says a machine is late, and a second
    # observation says it was still late when we looked again.
    compute.reconciliation.reconcile_pooled_capacity(now=moment + timedelta(seconds=200))
    moment += timedelta(seconds=301)
    compute.reconciliation.reconcile_pooled_capacity(now=moment)
    relaunched = _mark_open_record_booting(service_context, pool.id, at=moment)
    assert relaunched.launch_attempt == 2

    compute.reconciliation.reconcile_pooled_capacity(now=moment + timedelta(seconds=200))
    moment += timedelta(seconds=301)
    ensure_calls_before_exhaustion = len(provider.ensure_calls)
    compute.reconciliation.reconcile_pooled_capacity(now=moment)
    with service_context.database.session() as session:
        degraded = ComputeUnitRepository(session).get(pool.id)
        [reclaimed] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert degraded is not None
    assert degraded.phase is ComputeUnitPhase.Degraded
    assert degraded.provider_state.degraded_reason == "bootstrap_launch_attempts_exhausted"
    assert reclaimed.status == "deleted"
    reclaimed_machine = _machine_of(service_context, reclaimed)
    assert reclaimed_machine.lifecycle is MachineLifecycle.Deleted
    assert reclaimed_machine.lifecycle_failure is MachineBootstrapFailureReason.BootstrapTimedOut
    assert reclaimed.terminating_reason == "bootstrap_deadline_exceeded"
    assert reclaimed.provider_storage_destroyed_at is not None
    assert provider.desired == 0
    assert len(provider.ensure_calls) == ensure_calls_before_exhaustion

    compute.reconciliation.reconcile_pooled_capacity(now=moment + timedelta(seconds=301))
    with service_context.database.session() as session:
        still_degraded = ComputeUnitRepository(session).get(pool.id)
    assert still_degraded is not None
    assert still_degraded.provider_state.degraded_reason == "bootstrap_launch_attempts_exhausted"
    assert provider.desired == 0
    assert len(provider.ensure_calls) == ensure_calls_before_exhaustion
    compute.reconciliation.reconcile_pooled_capacity(now=moment + timedelta(seconds=601))
    with service_context.database.session() as session:
        relaunching = ComputeUnitRepository(session).get(pool.id)
    assert relaunching is not None
    assert relaunching.provider_state.degraded_reason == "bootstrap_launch_attempts_exhausted"
    assert relaunching.provider_state.degraded_at == degraded.provider_state.degraded_at
    assert provider.desired == 0

    scaled = compute.scaling.scale_internal_unit(
        pool.workspace_id,
        pool.capacity_owner_id,
        1,
        before_mutation=_allow_scale,
    )
    assert scaled.provider_state.degraded_reason is None
    assert scaled.desired_machines == 1
    assert provider.desired == 1


def _serving_pool(
    service_context: ServiceContext,
    hooks: _SchedulerHooks,
    provider: _PooledProvider,
    *,
    now: datetime,
    reclaim: ComputeReclaimPolicy | None = None,
) -> tuple[ComputeServices, ComputeUnitRecord]:
    _seed_connection(service_context)
    compute = pooled_service(
        service_context,
        provider,
        scheduler_hooks=hooks,
        reclaim=reclaim if reclaim is not None else ComputeReclaimPolicy(),
    )
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_pooled_capacity(now=now)
    _seed_serving_machine(
        service_context,
        pool,
        hooks,
        machine_id="44444444-4444-4444-8444-444444444444",
        instance_id="i-00000000000000000",
        now=now,
    )
    # One pass with the machine serving, which is what ends its bootstrap.
    compute.reconciliation.reconcile_pooled_capacity(now=now + timedelta(seconds=30))
    return compute, pool


def test_a_machine_that_serves_is_not_reclaimed_when_its_worker_record_lapses(
    service_context: ServiceContext,
) -> None:

    started_at = datetime.now(UTC)
    hooks = _SchedulerHooks(intake_observing_since=started_at - timedelta(hours=1))
    provider = _PooledProvider()
    compute, pool = _serving_pool(service_context, hooks, provider, now=started_at)

    served = _open_record(service_context, pool.id)
    assert served is not None
    assert served.first_served_at is not None

    hooks.available_machines.clear()
    hooks.unknown_machines.add("44444444-4444-4444-8444-444444444444")
    for offset in range(1, 12):
        compute.reconciliation.reconcile_pooled_capacity(now=started_at + timedelta(minutes=offset))

    survived = _open_record(service_context, pool.id)
    assert survived is not None
    assert survived.id == served.id
    assert survived.unserved_observations == 0
    assert provider.release_calls == []


def test_a_machine_that_served_and_stopped_is_reclaimed_once_its_window_passes(
    service_context: ServiceContext,
) -> None:

    started_at = datetime.now(UTC)
    hooks = _SchedulerHooks(intake_observing_since=started_at - timedelta(hours=1))
    provider = _PooledProvider()
    # One attempt, so the pool degrades instead of relaunching into the same row:
    # a reclaim and its replacement land in one pass, and the reason the machine
    # was taken away is only readable while no replacement has overwritten it.
    compute, pool = _serving_pool(
        service_context,
        hooks,
        provider,
        now=started_at,
        reclaim=ComputeReclaimPolicy(max_launch_attempts=1),
    )
    served = _open_record(service_context, pool.id)
    assert served is not None

    hooks.available_machines.clear()
    for offset in range(1, 13):
        compute.reconciliation.reconcile_pooled_capacity(now=started_at + timedelta(minutes=offset))

    with service_context.database.session() as session:
        records = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    reclaimed = next(item for item in records if item.id == served.id)
    reclaimed_machine = _machine_of(service_context, reclaimed)
    assert reclaimed_machine.lifecycle_failure is MachineBootstrapFailureReason.ServiceLost
    assert reclaimed_machine.lifecycle in {MachineLifecycle.Terminating, MachineLifecycle.Deleted}
    assert provider.release_calls == [served.instance_id]


def test_a_control_plane_that_just_started_reclaims_nothing(
    service_context: ServiceContext,
) -> None:

    started_at = datetime.now(UTC)
    hooks = _SchedulerHooks(intake_observing_since=started_at - timedelta(hours=1))
    provider = _PooledProvider()
    compute, pool = _serving_pool(service_context, hooks, provider, now=started_at)
    served = _open_record(service_context, pool.id)
    assert served is not None

    hooks.available_machines.clear()
    restarted_at = started_at + timedelta(hours=6)
    hooks.intake_observing_since = restarted_at
    for offset in range(1, 13):
        compute.reconciliation.reconcile_pooled_capacity(
            now=restarted_at + timedelta(seconds=offset * 30)
        )

    survived = _open_record(service_context, pool.id)
    assert survived is not None
    assert survived.id == served.id
    assert provider.release_calls == []
