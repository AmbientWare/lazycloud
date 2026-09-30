from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from compute.offers import ReservationStatus
from compute.providers import ProviderUnitRequest
from compute.service import ComputeServices
from database.context import ServiceContext
from database.repositories.compute import (
    ComputeCapacityOperationRecord,
    ComputeCapacityOperationRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import ContainerRepository, MachineRepository
from database.repositories.source_cache import SourceCacheCleanupRepository
from packages.compute.tests.pooled_fixtures import (
    _allow_scale,
    _AsyncScaleDownProvider,
    _bind_test_machine,
    _bootstrap,
    _MutationLeases,
    _PooledProvider,
    _Resolver,
    _seed_connection,
    pooled_service,
    prepare_unit,
)
from shared.capacity import CapacityAcquisitionShape, CapacityOperationStatus
from shared.compute_fleet import Machine, MachineLifecycle, ResourceStatus
from shared.compute_policy import ComputeUnitPhase, ComputeUnitRecord
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.source_cache_cleanup import WorkerCacheGenerationState


def test_internal_pool_scale_follows_demand_beyond_previous_fleet_ceiling(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context, platform_fleet=True)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=0)
    scaled = compute.scaling.scale_internal_unit(
        pool.workspace_id, pool.capacity_owner_id, 60, before_mutation=_allow_scale
    )
    assert scaled.desired_machines == 60
    assert provider.desired == 60


def test_scale_zero_persists_intent_and_releases_operations_before_provider_mutation(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    leases = _MutationLeases()
    compute = ComputeServices.create(
        service_context,
        provider_resolver=_Resolver(provider, service_context),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=leases,
    )
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_pooled_capacity()
    started_at = datetime(2026, 7, 22, 12, tzinfo=UTC)
    before = compute.providers.get_internal_unit(pool.workspace_id, pool.capacity_owner_id)
    guard_observations: list[int] = []

    def guard(current: ComputeUnitRecord) -> None:
        assert current.capacity_owner_id in leases.held
        guard_observations.append(current.desired_machines)

    def inspect_durable_intent(request: ProviderUnitRequest) -> None:
        with service_context.database.session() as session:
            durable = ComputeUnitRepository(session).get(request.unit_id)
            open_operations = ComputeCapacityOperationRepository(session).list_open_for_owner(
                pool.capacity_owner_id
            )
        assert durable is not None
        assert durable.desired_machines == 0
        assert durable.generation == before.generation + 1
        assert durable.phase is ComputeUnitPhase.Updating
        assert open_operations == []

    provider.before_capacity = inspect_durable_intent
    scaled = compute.scaling.scale_internal_unit(
        pool.workspace_id,
        pool.capacity_owner_id,
        0,
        before_mutation=guard,
        now=started_at,
    )

    assert guard_observations == [1]
    assert leases.acquired[-1] == pool.capacity_owner_id
    assert leases.dispatch_acquired[-1] == pool.capacity_owner_id
    assert scaled.desired_machines == 0
    assert scaled.observed_machines == 0
    assert scaled.phase is ComputeUnitPhase.Ready


def test_scale_zero_retains_degraded_intent_and_repairs_provider_failure(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_pooled_capacity()
    provider.capacity_failure = RuntimeError("provider request failed")

    with pytest.raises(UpstreamUnavailableError, match="provider capacity update failed"):
        compute.scaling.scale_internal_unit(
            pool.workspace_id,
            pool.capacity_owner_id,
            0,
            before_mutation=_allow_scale,
        )

    degraded = compute.providers.get_internal_unit(pool.workspace_id, pool.capacity_owner_id)
    assert degraded.desired_machines == 0
    assert degraded.observed_machines == 1
    assert degraded.phase is ComputeUnitPhase.Degraded
    provider.capacity_failure = None

    repaired = compute.scaling.scale_internal_unit(
        pool.workspace_id,
        pool.capacity_owner_id,
        0,
        before_mutation=_allow_scale,
    )

    assert provider.desired == 0
    assert repaired.desired_machines == 0
    assert repaired.observed_machines == 0
    assert repaired.phase is ComputeUnitPhase.Ready


def test_internal_pool_scale_maps_mutation_coordinator_failure(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()

    def fail_acquire(_capacity_owner_id: str) -> None:
        raise RuntimeError("redis unavailable")

    leases = _MutationLeases(on_acquire=fail_acquire)
    compute = ComputeServices.create(
        service_context,
        provider_resolver=_Resolver(provider, service_context),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=leases,
    )
    pool = prepare_unit(compute, desired=1)

    with pytest.raises(UpstreamUnavailableError, match="mutation lease is unavailable"):
        compute.scaling.scale_internal_unit(
            pool.workspace_id,
            pool.capacity_owner_id,
            0,
            before_mutation=_allow_scale,
        )


def test_scale_zero_skips_provider_only_after_durable_convergence(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=0)
    compute.reconciliation.reconcile_pooled_capacity()
    first = compute.scaling.scale_internal_unit(
        pool.workspace_id,
        pool.capacity_owner_id,
        0,
        before_mutation=_allow_scale,
    )
    generation = first.generation

    second = compute.scaling.scale_internal_unit(
        pool.workspace_id,
        pool.capacity_owner_id,
        0,
        before_mutation=_allow_scale,
    )

    # Control-plane startup drives every workspace through this path on every
    # boot. A pool already durably at zero must cost a describe and nothing else
    # — no capacity write, no generation churn.
    assert provider.capacity_calls == []
    assert second.generation == generation
    assert second == first


def test_scale_zero_repairs_fresh_provider_drift_without_restoring_nonzero_intent(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=0)
    compute.reconciliation.reconcile_pooled_capacity()
    converged = compute.scaling.scale_internal_unit(
        pool.workspace_id,
        pool.capacity_owner_id,
        0,
        before_mutation=_allow_scale,
    )
    provider.desired = 1

    def inspect_repair_intent(request: ProviderUnitRequest) -> None:
        with service_context.database.session() as session:
            durable = ComputeUnitRepository(session).get(request.unit_id)
        assert durable is not None
        assert durable.desired_machines == 0
        assert durable.observed_machines == 1
        assert durable.phase is ComputeUnitPhase.Updating

    provider.before_capacity = inspect_repair_intent
    repaired = compute.scaling.scale_internal_unit(
        pool.workspace_id,
        pool.capacity_owner_id,
        0,
        before_mutation=_allow_scale,
    )

    assert provider.desired == 0
    assert repaired.generation == converged.generation + 1
    assert repaired.desired_machines == 0
    assert repaired.observed_machines == 0
    assert repaired.phase is ComputeUnitPhase.Ready


def test_zero_capacity_reconciliation_releases_drift_without_a_supplier_catalog(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=0)
    provider.catalog_failure = RuntimeError("supplier catalog unavailable")
    provider.desired = 1

    compute.reconciliation.reconcile_pooled_capacity()

    with service_context.database.session() as session:
        reconciled = ComputeUnitRepository(session).get(pool.id)
    assert reconciled is not None
    assert reconciled.phase is ComputeUnitPhase.Ready
    assert reconciled.desired_machines == 0
    assert reconciled.observed_machines == 0
    assert provider.desired == 0


def test_partial_scale_down_releases_capacity_without_a_supplier_catalog(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=3)
    compute.reconciliation.reconcile_pooled_capacity()
    assert provider.desired == 3
    provider.catalog_failure = RuntimeError("supplier catalog unavailable")

    reduced = compute.scaling.scale_internal_unit(
        pool.workspace_id,
        pool.capacity_owner_id,
        2,
        before_mutation=_allow_scale,
    )

    assert reduced.desired_machines == 2
    assert reduced.observed_machines == 2
    assert provider.desired == 2
    assert compute.providers.get_internal_unit(pool.workspace_id, pool.capacity_owner_id) == reduced

    with pytest.raises(UpstreamUnavailableError):
        compute.scaling.scale_internal_unit(
            pool.workspace_id,
            pool.capacity_owner_id,
            3,
            before_mutation=_allow_scale,
        )

    assert provider.desired == 2
    assert compute.providers.get_internal_unit(pool.workspace_id, pool.capacity_owner_id) == reduced


def test_scale_zero_terminalizes_missing_provider_instance_projections(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_pooled_capacity()
    missing_since = datetime.now(UTC) - timedelta(minutes=5)
    with service_context.database.session() as session:
        repository = ComputeProviderInstanceRepository(session)
        [active] = repository.list_for_pool(pool.id)
        pending_with_instance = active.model_copy(
            update={
                "id": str(uuid4()),
                "status": "pending",
                "instance_id": "i-pending0000000001",
                "machine_id": None,
                "missing_since": missing_since,
                "storage_volume_ids": (),
            }
        )
        planned_without_instance = active.model_copy(
            update={
                "id": str(uuid4()),
                "status": "pending",
                "instance_id": None,
                "machine_id": None,
                "missing_since": missing_since,
                "storage_volume_ids": (),
            }
        )
        repository.upsert(pending_with_instance)
        repository.upsert(planned_without_instance)
        before = {item.id: item.created_at for item in repository.list_for_pool(pool.id)}
        orphaned_operation = ComputeCapacityOperationRepository(session).upsert(
            ComputeCapacityOperationRecord(
                shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32_768),
                id=str(uuid4()),
                workspace_id=pool.workspace_id,
                pool_id=pool.id,
                capacity_owner_id=pool.capacity_owner_id,
                reservation_id=str(uuid4()),
                operation_id=str(uuid4()),
                desired_unit=1,
                status=CapacityOperationStatus.Requested,
                owns_capacity=True,
            )
        )

    scaled = compute.scaling.scale_internal_unit(
        pool.workspace_id,
        pool.capacity_owner_id,
        0,
        before_mutation=_allow_scale,
    )

    with service_context.database.session() as session:
        after = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        released_operation = ComputeCapacityOperationRepository(session).get(
            orphaned_operation.capacity_owner_id,
            orphaned_operation.operation_id,
        )
    assert scaled.desired_machines == 0
    assert scaled.observed_machines == 0
    assert scaled.phase is ComputeUnitPhase.Ready
    assert {item.id: item.created_at for item in after} == before
    assert {item.status for item in after} == {"deleted"}
    assert all(item.terminated_reason == "provider_instance_missing" for item in after)
    assert all(item.provider_storage_destroyed_at is not None for item in after)
    assert released_operation is not None
    assert released_operation.status == "released"
    assert released_operation.release_desired_unit == 0


def test_reconcile_rereads_zero_intent_after_capacity_owner_lease(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    scale_compute = pooled_service(service_context, provider)
    pool = prepare_unit(scale_compute, desired=1)
    scale_compute.reconciliation.reconcile_pooled_capacity()
    provider.ensure_calls.clear()
    reconcile_leases = _MutationLeases()

    def scale_before_reconcile(capacity_owner_id: str) -> None:
        if capacity_owner_id != pool.capacity_owner_id:
            return
        reconcile_leases.on_acquire = None
        scale_compute.scaling.scale_internal_unit(
            pool.workspace_id,
            pool.capacity_owner_id,
            0,
            before_mutation=_allow_scale,
        )

    reconcile_leases.on_acquire = scale_before_reconcile
    reconciler = ComputeServices.create(
        service_context,
        provider_resolver=_Resolver(provider, service_context),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=reconcile_leases,
    )

    reconciler.reconciliation.reconcile_pooled_capacity()

    assert provider.desired == 0
    durable = reconciler.providers.get_internal_unit(pool.workspace_id, pool.capacity_owner_id)
    assert durable.desired_machines == 0
    assert durable.observed_machines == 0


def test_pooled_scale_down_waits_for_exact_volume_absence(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    instance_id = "i-00000000000000000"
    provider = _PooledProvider(lingering_storage={instance_id})
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=1)
    started_at = datetime.now(UTC)
    compute.reconciliation.reconcile_pooled_capacity(now=started_at)
    machine_id = "33333333-3333-4333-8333-333333333333"
    generation_id = "44444444-4444-4444-8444-444444444444"
    cache_created_at = datetime.now(UTC)
    with service_context.database.session() as session:
        MachineRepository(session).upsert(
            Machine(
                id=machine_id,
                placement=pool.placement,
                provider="agent",
                status=ResourceStatus.Running,
                lifecycle=MachineLifecycle.Joining,
            ),
            workspace_id=pool.workspace_id,
        )
        bound = _bind_test_machine(session, pool.id, instance_id, machine_id)
        assert bound is not None
        SourceCacheCleanupRepository(session).register_generation(
            generation_id,
            worker_id=f"worker-{machine_id}",
            storage_id=f"machine:{machine_id}",
            workspace_id=pool.workspace_id,
            now=cache_created_at,
        )
    scaling = compute.scaling.scale_internal_unit(
        pool.workspace_id,
        pool.capacity_owner_id,
        0,
        before_mutation=_allow_scale,
    )
    assert scaling.phase is ComputeUnitPhase.Updating

    compute.reconciliation.reconcile_pooled_capacity(now=started_at + timedelta(seconds=121))
    with service_context.database.session() as session:
        [lingering] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        active_generation = SourceCacheCleanupRepository(session).get_generation(generation_id)
        updating_pool = ComputeUnitRepository(session).get(pool.id)
    assert lingering.status != "deleted"
    assert lingering.storage_volume_ids == ("vol-00000000000000000",)
    assert active_generation is not None
    assert active_generation.state is not WorkerCacheGenerationState.Retired
    assert updating_pool is not None
    assert updating_pool.phase is ComputeUnitPhase.Updating

    with service_context.database.session() as session:
        ComputeProviderInstanceRepository(session).upsert(
            lingering.model_copy(update={"status": ReservationStatus.Failed.value})
        )
    with pytest.raises(UpstreamUnavailableError, match="capacity release is in progress"):
        compute.removal.delete_unit(pool.capacity_owner_id, workspace=pool.workspace_id)
    with service_context.database.session() as session:
        deleting_pool = ComputeUnitRepository(session).get(pool.id)
    assert deleting_pool is not None and deleting_pool.phase is ComputeUnitPhase.Deleting

    provider.lingering_storage.clear()
    compute.reconciliation.reconcile_unit_capacity(pool.id, now=started_at)
    with service_context.database.session() as session:
        [destroyed] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        retired_generation = SourceCacheCleanupRepository(session).get_generation(generation_id)
        ready_pool = ComputeUnitRepository(session).get(pool.id)
    assert destroyed.status == "deleted"
    observed_at = destroyed.provider_storage_destroyed_at
    assert observed_at is not None
    assert observed_at >= cache_created_at
    assert retired_generation is not None
    assert retired_generation.state is WorkerCacheGenerationState.Retired
    assert ready_pool is not None
    assert ready_pool.phase is ComputeUnitPhase.Deleted

    provider.storage_failure = RuntimeError("provider storage API unavailable")
    assert compute.reconciliation.reconcile_unit_capacity(pool.id) is None
    with service_context.database.session() as session:
        [preserved] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        preserved_generation = SourceCacheCleanupRepository(session).get_generation(generation_id)
    assert preserved.updated_at == destroyed.updated_at
    assert preserved.provider_storage_destroyed_at == destroyed.provider_storage_destroyed_at
    assert preserved.terminated_reason == destroyed.terminated_reason
    assert preserved_generation is not None
    assert preserved_generation.state is WorkerCacheGenerationState.Retired


def test_pooled_scale_down_projects_updating_during_provider_termination(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _AsyncScaleDownProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_pooled_capacity()

    scaling = compute.scaling.scale_internal_unit(
        pool.workspace_id,
        pool.capacity_owner_id,
        0,
        before_mutation=_allow_scale,
    )

    assert scaling.desired_machines == 0
    assert scaling.observed_machines == 1
    assert scaling.phase is ComputeUnitPhase.Updating
    with service_context.database.session() as session:
        [retiring] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert retiring.storage_volume_ids == ("vol-00000000000000000",)
    with service_context.database.session() as session:
        ComputeProviderInstanceRepository(session).upsert(
            retiring.model_copy(update={"status": ReservationStatus.Failed.value})
        )
    compute.scaling.scale_internal_unit(
        pool.workspace_id, pool.capacity_owner_id, 0, before_mutation=_allow_scale
    )
    with service_context.database.session() as session:
        [retained] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert retained.storage_volume_ids == retiring.storage_volume_ids
    assert retained.launch_attempt == retiring.launch_attempt
    with pytest.raises(UpstreamUnavailableError, match="capacity release is in progress"):
        compute.removal.delete_unit(pool.capacity_owner_id, workspace=pool.workspace_id)
    with service_context.database.session() as session:
        deleting = ComputeUnitRepository(session).get(pool.id)
    assert deleting is not None and deleting.phase is ComputeUnitPhase.Deleting


def test_clearing_warm_capacity_releases_internal_pool_machines(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    baseline = compute.provisioning.reconcile_aws_default_capacity(
        workspace="default",
        region="us-east-1",
        instance_type="m7i.xlarge",
        initial_machines=1,
        min_machines=1,
        min_free_cpu_millicores=1_000,
        min_free_memory_mib=1_024,
        root_volume_gib=200,
        idle_timeout_seconds=300,
    )
    compute.reconciliation.reconcile_pooled_capacity()
    assert baseline.desired_machines == 1
    assert provider.desired == 1

    compute.provisioning.clear_aws_default_capacity(
        workspace=baseline.workspace_id, release_capacity=True
    )

    with service_context.database.session() as session:
        drained = ComputeUnitRepository(session).get(baseline.id)
    assert drained is not None
    assert drained.min_machines == 0
    assert drained.desired_machines == 0
    assert provider.desired == 0


def test_customer_baseline_releases_its_floor_and_preserves_demand(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    resolver = _Resolver(provider, service_context)
    compute = ComputeServices.create(
        service_context,
        provider_resolver=resolver,
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )

    def reconcile(*, floor: int) -> ComputeUnitRecord:
        return compute.provisioning.reconcile_aws_default_capacity(
            workspace="default",
            region="us-east-1",
            instance_type="m7i.xlarge",
            initial_machines=floor,
            min_machines=floor,
            min_free_cpu_millicores=1_000,
            min_free_memory_mib=1_024,
            root_volume_gib=200,
            idle_timeout_seconds=300,
        )

    assert reconcile(floor=1).desired_machines == 1
    grown = prepare_unit(compute, desired=5)
    assert grown.desired_machines == 5

    # Lowering the floor releases exactly the capacity that floor was holding and
    # leaves demand-grown capacity to the scheduler's own owners.
    assert reconcile(floor=0).desired_machines == 4
    assert reconcile(floor=3).desired_machines == 4


def test_clearing_warm_capacity_preserves_work_from_another_workspace(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = compute.provisioning.reconcile_aws_default_capacity(
        workspace="default",
        region="us-east-1",
        instance_type="m7i.xlarge",
        initial_machines=1,
        min_machines=1,
        min_free_cpu_millicores=1_000,
        min_free_memory_mib=1_024,
        root_volume_gib=200,
        idle_timeout_seconds=300,
    )
    compute.reconciliation.reconcile_pooled_capacity()
    assert provider.desired == 1

    machine_id = "44444444-4444-4444-8444-444444444444"
    with service_context.database.session() as session:
        customer_workspace = WorkspaceRepository(session).create(name="capacity-customer")
        MachineRepository(session).upsert(
            Machine(
                id=machine_id,
                placement=pool.placement,
                provider=pool.provider_ref,
                status=ResourceStatus.Running,
                lifecycle=MachineLifecycle.Ready,
            ),
            workspace_id=pool.workspace_id,
        )
        bound = _bind_test_machine(session, pool.id, "i-00000000000000000", machine_id)
        assert bound is not None
        ContainerRepository(session).upsert(
            ContainerRecord(
                id="55555555-5555-4555-8555-555555555555",
                name="container-running",
                image="",
                command=[],
                workspace_id=customer_workspace.id,
                runtime_machine_id=machine_id,
                status=ContainerStatus.Running,
            )
        )

    compute.provisioning.clear_aws_default_capacity(
        workspace=pool.workspace_id, release_capacity=True
    )

    with service_context.database.session() as session:
        held = ComputeUnitRepository(session).get(pool.id)
    assert held is not None
    # The provider scales in by picking its own victim, so the running workload is
    # only safe while the floor still holds its machine.
    assert held.desired_machines == 1
    assert provider.desired == 1
    # The floor is gone, which is what lets the drain owner release the machine
    # once the work finishes.
    assert held.min_machines == 0

    with pytest.raises(ConflictError, match="compute pool still has active workloads"):
        compute.scaling.scale_internal_unit(
            pool.workspace_id,
            pool.capacity_owner_id,
            0,
            before_mutation=_allow_scale,
        )
    assert provider.desired == 1
