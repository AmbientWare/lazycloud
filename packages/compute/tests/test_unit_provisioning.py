from __future__ import annotations

from uuid import uuid4

import pytest
from compute.provider_state import ProviderUnitStateService
from compute.providers import (
    ProviderOfferEligibility,
    ResolvedComputeProvider,
    ResolvedProviderPolicy,
)
from compute.service import ComputeServices
from compute.supplier_costs import SupplierCostInspectionService
from database.context import ServiceContext
from database.repositories.compute import ComputeUnitRepository
from database.repositories.identity import WorkspaceRepository
from identity.platform import PlatformNamespaceService
from packages.compute.tests.pooled_fixtures import (
    _allow_scale,
    _bootstrap,
    _MutationLeases,
    _offer,
    _PooledProvider,
    _Resolver,
    _seed_connection,
    pooled_service,
    prepare_unit,
)
from provider_aws.managed_pool import Boto3AwsManagedPoolClientProvider
from provider_clients.workspace_compute import WorkspaceComputeProviderResolver
from shared.capacity import (
    CapacityAcquisitionRequest,
    CapacityAcquisitionShape,
    CapacityAcquisitionStatus,
)
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeResourceRequirements,
    ComputeUnitPhase,
    ComputeUnitRecord,
    UnitName,
)
from shared.errors import ConflictError, NotFoundError
from shared.placement import Placement
from shared.supplier_costs import SupplierCostTerms


def test_provider_launch_checkpoint_survives_stale_snapshot_and_rejects_stale_writer(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=1)
    request = compute.providers.provider_unit_request(pool, provider.offer)
    checkpoints = ProviderUnitStateService(service_context.database)
    initial = checkpoints.load(request)
    intent = initial.model_copy(
        update={"revision": initial.revision + 1, "attributes": {"launch_token": "durable-intent"}}
    )
    checkpoints.save(request, expected=initial, state=intent)
    with pytest.raises(ConflictError):
        checkpoints.save(request, expected=initial, state=intent)
    with service_context.database.session() as session:
        assert (
            ComputeUnitRepository(session).apply_provider_state(
                pool.id,
                generation=pool.generation,
                observed_machines=0,
                phase=ComputeUnitPhase.Ready,
                provider_state=initial,
            )
            is None
        )
    compute.providers.mark_pooled_capacity_degraded(pool)
    assert checkpoints.load(request) == intent


def test_platform_capacity_reconciles_without_an_aws_connection(
    service_context: ServiceContext,
) -> None:
    workspace_id = PlatformNamespaceService(service_context.database).get().id
    offer = _offer().model_copy(update={"provider": "aws:platform"})
    provider = _PooledProvider(offer=offer)
    resolved = ResolvedComputeProvider(
        ref=offer.provider,
        capacity_mode=ComputeCapacityMode.Pooled,
        pooled=provider,
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
                ),
            ),
        ),
    )
    resolver = WorkspaceComputeProviderResolver(
        connections=lambda _workspace: (),
        capacity_workspace=lambda _connection: workspace_id,
        binaries_by_region={},
        client_provider=Boto3AwsManagedPoolClientProvider.from_default_chain(),
        platform_providers=lambda: (resolved,),
    )
    compute = ComputeServices.create(
        service_context,
        provider_resolver=resolver,
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    unit = compute.provisioning.prepare_pooled_capacity(
        workspace=workspace_id,
        requirements=ComputeResourceRequirements(cpu_millicores=1000, memory_mb=1024),
        region=offer.region,
        desired_machines=1,
        root_volume_gib=200,
    )
    assert unit.provider_connection_id is None
    assert unit.platform_fleet
    compute.reconciliation.reconcile_pooled_capacity()
    restarted = ComputeServices.create(
        service_context,
        provider_resolver=resolver,
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    assert restarted.reconciliation.reconcile_pooled_capacity()[0].id == unit.id
    scaled = restarted.scaling.scale_internal_unit(
        workspace_id, unit.id, 3, before_mutation=_allow_scale
    )
    assert scaled.desired_machines == 3


def test_acquired_node_costs_survive_offer_changes_and_catalog_loss(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    original = SupplierCostTerms(
        compute_hourly_micros=340_000,
        root_disk_hourly_micros=20_000,
        public_ipv4_hourly_micros=5_000,
        setup_micros=0,
        billing_minimum_seconds=0,
        billing_quantum_seconds=60,
    )
    provider = _PooledProvider(offer=_offer().model_copy(update={"cost_terms": original}))
    compute = pooled_service(service_context, provider)
    unit = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_pooled_capacity()
    replacement = original.model_copy(update={"compute_hourly_micros": 680_000})
    provider.offer = provider.offer.model_copy(update={"cost_terms": replacement})
    prepare_unit(compute, desired=2)
    compute.reconciliation.reconcile_pooled_capacity()
    provider.catalog_failure = RuntimeError("supplier catalog unavailable")
    compute.reconciliation.reconcile_pooled_capacity()

    inspection = SupplierCostInspectionService(service_context.database)
    report = inspection.inspect(workspace_id=unit.workspace_id, unit_id=unit.id)
    assert report.offer.terms == replacement
    assert {node.provider_instance_id: node.costs.terms for node in report.nodes} == {
        "i-00000000000000000": original,
        "i-00000000000000001": replacement,
    }
    with pytest.raises(NotFoundError):
        inspection.inspect(workspace_id=str(uuid4()), unit_id=unit.id)


def test_internal_pool_lookup_is_workspace_scoped(service_context: ServiceContext) -> None:
    _seed_connection(service_context)
    compute = ComputeServices.create(
        service_context,
        provider_resolver=_Resolver(_PooledProvider(), service_context),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = prepare_unit(compute, desired=0)

    with pytest.raises(NotFoundError, match="compute unit not found"):
        compute.providers.get_internal_unit(str(uuid4()), pool.capacity_owner_id)
    with service_context.database.session() as session:
        other = WorkspaceRepository(session).create(name="other-compute-owner")
    with pytest.raises(NotFoundError, match="compute unit not found"):
        compute.removal.delete_unit(pool.capacity_owner_id, workspace=other.id)
    assert compute.providers.get_internal_unit(pool.workspace_id, pool.capacity_owner_id) == pool


def test_aws_default_capacity_is_one_durable_floor_preserved_by_placement(
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
    with service_context.database.session() as session:
        sibling_id = str(uuid4())
        ComputeUnitRepository(session).upsert(
            baseline.model_copy(
                update={
                    "id": sibling_id,
                    "capacity_owner_id": sibling_id,
                    "name": "larger-demand-owned-cpu",
                    "placement": Placement.machine("larger-demand-owned-cpu"),
                    "selector": "larger-demand-owned-cpu",
                    "capability_key": f"{baseline.capability_key}:larger",
                    "desired_machines": 1,
                    "initial_machines": 1,
                    "min_machines": 1,
                    "min_free_cpu_millicores": 2_000,
                    "min_free_memory_mib": 2_048,
                }
            )
        )
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
    placed = prepare_unit(compute, desired=0)

    with service_context.database.session() as session:
        internal = ComputeUnitRepository(session).list_internal(workspace_id=baseline.workspace_id)
    units = {item.name: item for item in internal}
    assert placed.id == baseline.id
    # Demand-driven placement never shrinks a pool it did not size.
    assert placed.desired_machines == 1
    assert [pool.id for pool in internal if pool.min_machines > 0] == [baseline.id]
    kept = units[baseline.name]
    assert kept.initial_machines == 1
    assert kept.min_machines == 1
    assert kept.min_free_cpu_millicores == 1_000
    assert kept.min_free_memory_mib == 1_024
    larger = units[UnitName("larger-demand-owned-cpu")]
    assert larger.initial_machines == 0
    assert larger.min_machines == 0
    assert larger.min_free_cpu_millicores == 0
    assert larger.min_free_memory_mib == 0

    compute.provisioning.clear_aws_default_capacity(workspace="default", release_capacity=False)
    with service_context.database.session() as session:
        cleared = ComputeUnitRepository(session).get(baseline.id)
    assert cleared is not None
    assert cleared.min_machines == 0
    assert cleared.initial_machines == 0
    assert cleared.min_free_cpu_millicores == 0
    assert cleared.min_free_memory_mib == 0
    drained = compute.scaling.scale_internal_unit(
        baseline.workspace_id,
        baseline.capacity_owner_id,
        0,
        before_mutation=_allow_scale,
    )
    assert drained.desired_machines == 0


def test_pooled_capacity_does_not_import_provider_surplus_into_logical_intent(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=0)
    compute.reconciliation.reconcile_pooled_capacity()
    provider.desired = 3

    planned = compute.capacity.ensure_capacity(
        CapacityAcquisitionRequest(
            capacity_owner_id=pool.capacity_owner_id,
            reservation_id=str(uuid4()),
            operation_id=str(uuid4()),
            shape=CapacityAcquisitionShape(
                cpu_millicores=4_000,
                memory_mib=32 * 1_024,
            ),
        )
    )

    assert planned.status is CapacityAcquisitionStatus.ExistingPending
    assert planned.desired_unit == 1
    assert provider.desired == 3
    durable = compute.providers.get_internal_unit(pool.workspace_id, pool.capacity_owner_id)
    assert durable.desired_machines == 1


def test_platform_growth_checks_workload_rates_and_new_quotes_without_blocking_drain(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context, platform_fleet=True)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=0)
    request = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32 * 1_024),
        workload_preemptible=True,
    )
    assert (
        compute.capacity.ensure_capacity(request).status
        is CapacityAcquisitionStatus.TemporarilyUnavailable
    )
    assert provider.desired == 0
    assert (
        compute.capacity.ensure_capacity(
            request.model_copy(update={"workload_preemptible": False})
        ).status
        is CapacityAcquisitionStatus.Requested
    )
    assert provider.desired == 1

    provider.offer = provider.offer.model_copy(
        update={
            "cost_terms": SupplierCostTerms(
                compute_hourly_micros=10_000_000,
                root_disk_hourly_micros=0,
                public_ipv4_hourly_micros=0,
            )
        }
    )
    with pytest.raises(ConflictError):
        compute.scaling.scale_internal_unit(
            pool.workspace_id, pool.capacity_owner_id, 2, before_mutation=_allow_scale
        )
    assert provider.desired == 1
    compute.scaling.scale_internal_unit(
        pool.workspace_id, pool.capacity_owner_id, 0, before_mutation=_allow_scale
    )
    assert provider.desired == 0


def test_capacity_asked_for_again_revives_a_deleted_pool(
    service_context: ServiceContext,
) -> None:

    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)

    def ask_for_the_floor() -> ComputeUnitRecord:
        return compute.provisioning.reconcile_aws_default_capacity(
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

    unit = ask_for_the_floor()
    with service_context.database.session() as session:
        repository = ComputeUnitRepository(session)
        stored = repository.get(unit.id)
        assert stored is not None
        repository.upsert(
            stored.model_copy(
                update={
                    "phase": ComputeUnitPhase.Deleted,
                    "status": ComputeUnitPhase.Deleted.value,
                }
            )
        )

    revived = ask_for_the_floor()

    assert revived.phase is not ComputeUnitPhase.Deleted
    assert revived.min_machines == 1
    assert revived.generation == unit.generation + 1
