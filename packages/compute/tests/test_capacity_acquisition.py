from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from compute.providers import (
    ProviderOfferEligibility,
    ProviderUnitRequest,
    ResolvedComputeProvider,
    ResolvedProviderPolicy,
)
from compute.request_placement import ComputeCapacityPlacementRequest
from compute.service import ComputeServices
from database.context import ServiceContext
from database.repositories.aws_connections import AwsAccountConnectionRepository
from database.repositories.compute import ComputeCapacityOperationRepository, ComputeUnitRepository
from identity.platform import PlatformNamespaceService
from packages.compute.tests.pooled_fixtures import (
    _CONNECTION_ID,
    _bootstrap,
    _MutationLeases,
    _offer,
    _PooledProvider,
    _Resolver,
    _SchedulerHooks,
    _seed_connection,
    _seed_serving_machine,
    pooled_service,
    prepare_unit,
)
from provider_aws.instance_catalog import AWS_ALLOWED_OFFERS
from provider_aws.managed_pool import AwsManagedPoolBinaries, Boto3AwsManagedPoolClientProvider
from provider_clients.workspace_compute import WorkspaceComputeProviderResolver
from scheduler.capacity_reservations import (
    CapacityAcquisitionStatus as SchedulerCapacityAcquisitionStatus,
)
from scheduler.capacity_reservations import (
    CapacityRequestShape,
    CapacityReservationService,
    CapacityReservationStatus,
    ComputeUnitCapacityController,
    RedisCapacityReservationRepository,
)
from scheduler.state import RedisSchedulerWorkerRepository
from shared.aws_connections import (
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
)
from shared.capacity import (
    CapacityAcquisitionRequest,
    CapacityAcquisitionShape,
    CapacityAcquisitionStatus,
    CapacityFailureCode,
    CapacityOperationStatus,
    CapacityReleaseRequest,
)
from shared.compute_enrollment import agent_machine_worker_id
from shared.compute_policy import ComputeCapacityMode, ComputeResourceRequirements, ComputeUnitPhase
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.placement import Placement
from shared.scheduling import SchedulerWorkerRequest
from shared.supplier_costs import SupplierCostTerms
from tests.real_redis import RealRedisActors


@pytest.mark.parametrize("market_state", ["open", "cooling"])
def test_purchase_admission_respects_market_cooldown(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
    market_state: str,
) -> None:
    with service_context.database.session() as session:
        workspace_id = PlatformNamespaceService(service_context.database).get().id
    providers: list[ResolvedComputeProvider] = []
    for name, cost in (
        ("existing", 400_000),
        ("unused", 300_000),
        ("cheap", 200_000),
        ("unknown", None),
        ("unprofitable", 10_000_000),
        ("disabled", 1),
    ):
        offer = _offer().model_copy(
            update={
                "provider": f"aws:{name}",
                "cost_terms": SupplierCostTerms(
                    compute_hourly_micros=cost,
                    root_disk_hourly_micros=0,
                    public_ipv4_hourly_micros=0,
                ),
            }
        )
        providers.append(
            ResolvedComputeProvider(
                ref=offer.provider,
                capacity_mode=ComputeCapacityMode.Pooled,
                pooled=_PooledProvider(
                    offer=offer,
                    catalog_failure=RuntimeError("disabled catalog queried")
                    if name == "disabled"
                    else None,
                ),
                policy=ResolvedProviderPolicy(
                    purchases_enabled=name != "disabled",
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
        )
    resolver = WorkspaceComputeProviderResolver(
        connections=lambda _workspace: (),
        capacity_workspace=lambda _connection: workspace_id,
        binaries_by_region={},
        client_provider=Boto3AwsManagedPoolClientProvider.from_default_chain(),
        platform_providers=lambda: tuple(providers),
    )
    redis = real_redis_actors.client()
    reservations = RedisCapacityReservationRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    compute = ComputeServices.create(
        service_context,
        provider_resolver=resolver,
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=reservations,
    )
    requirements = ComputeResourceRequirements(cpu_millicores=1000, memory_mb=1024)
    existing = providers[0]
    assert existing.pooled is not None
    existing_offer = next(iter(existing.pooled.list_offers(root_volume_gib=200)))
    unit = compute.provisioning.prepare_pooled_offer(
        provider=existing, offer=existing_offer, requirements=requirements
    )
    placement = compute.placement
    candidates = placement.purchase_candidates(
        ComputeCapacityPlacementRequest(
            workspace_id=workspace_id,
            placement=Placement.platform(),
            requirements=requirements,
        )
    )
    with service_context.database.session() as session:
        assert [
            item.id
            for item in ComputeUnitRepository(session).list_internal(workspace_id=workspace_id)
        ] == [unit.id]
    now = datetime.now(UTC)
    if market_state == "cooling":
        candidates[0].prepare()
        with service_context.database.session() as session:
            units_repository = ComputeUnitRepository(session)
            failed = units_repository.get(candidates[0].capacity_owner_id)
            assert failed is not None
            units_repository.upsert(
                failed.model_copy(
                    update={
                        "phase": ComputeUnitPhase.Degraded,
                        "provider_state": failed.provider_state.model_copy(
                            update={
                                "degraded_reason": "provider_acquisition_rejected",
                                "degraded_at": now,
                            }
                        ),
                    }
                )
            )

    def controllers() -> tuple[ComputeUnitCapacityController, ...]:
        return tuple(
            ComputeUnitCapacityController(item.workspace_id, item, compute.capacity, workers, 0)
            for item in compute.units.platform_units()
        )

    service = CapacityReservationService(reservations, controllers)
    request = SchedulerWorkerRequest(
        fairness_account_id="test-account",
        workspace_id=workspace_id,
        stub_id=str(uuid4()),
        container_id=str(uuid4()),
        cpu_millicores=1_000,
        memory_mib=1_024,
        placement=Placement.platform(),
        timestamp=now,
    )
    acquired = service.acquire(request, purchases=lambda: candidates, now=now)
    with service_context.database.session() as session:
        units = ComputeUnitRepository(session).list_internal(workspace_id=workspace_id)
    assert acquired.status is SchedulerCapacityAcquisitionStatus.Requested
    purchased = next(item for item in units if item.capacity_owner_id == acquired.capacity_owner_id)
    assert purchased.provider_ref == ("aws:unused" if market_state == "cooling" else "aws:cheap")
    assert sum(item.desired_machines for item in units) == 1
    assert {item.provider_ref for item in units} == (
        {"aws:existing", "aws:cheap", "aws:unused"}
        if market_state == "cooling"
        else {"aws:existing", "aws:cheap"}
    )


def test_authorization_revalidation_preserves_pool_floor_and_owned_acquisition(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    compute = ComputeServices.create(
        service_context,
        provider_resolver=_Resolver(_PooledProvider(), service_context),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = prepare_unit(compute, desired=0)
    acquisition = CapacityAcquisitionRequest(
        capacity_owner_id=pool.id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32 * 1_024),
    )
    assert compute.capacity.ensure_capacity(acquisition).owns_capacity
    with service_context.database.session() as session:
        units = ComputeUnitRepository(session)
        current = units.get(pool.id)
        assert current is not None
        before = units.upsert(current.model_copy(update={"min_machines": 1, "initial_machines": 1}))
        operation = ComputeCapacityOperationRepository(session).get(
            pool.id, acquisition.operation_id
        )
        assert operation is not None
        connections = AwsAccountConnectionRepository(session)
        connection = connections.get(_CONNECTION_ID)
        assert connection is not None and connection.active_authorization is not None
        connections.save(
            connection.model_copy(
                update={
                    "phase": AwsAccountConnectionPhase.Validating,
                    "active_authorization": connection.active_authorization.model_copy(
                        update={"phase": AwsAccountAuthorizationPhase.Validating}
                    ),
                }
            )
        )

    def load_connections() -> tuple[AwsAccountConnection, ...]:
        with service_context.database.session() as session:
            current = AwsAccountConnectionRepository(session).get(_CONNECTION_ID)
        assert current is not None
        return (current,)

    resolver = WorkspaceComputeProviderResolver(
        connections=lambda _workspace: load_connections(),
        capacity_workspace=lambda _connection: pool.workspace_id,
        binaries_by_region={
            "us-east-1": AwsManagedPoolBinaries(
                agent_version="0.1.0",
                agent_sha256="a" * 64,
                cpu_ami_id="ami-0123456789abcdef0",
            )
        },
        client_provider=Boto3AwsManagedPoolClientProvider.from_default_chain(),
    )
    compute = ComputeServices.create(
        service_context,
        provider_resolver=resolver,
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    observed = compute.reconciliation.reconcile_unit_capacity(pool.id)
    assert observed is not None and observed.phase is before.phase
    blocked = compute.capacity.ensure_capacity(
        acquisition.model_copy(
            update={"reservation_id": str(uuid4()), "operation_id": str(uuid4())}
        )
    )
    assert blocked.status is CapacityAcquisitionStatus.TemporarilyUnavailable
    assert not blocked.owns_capacity
    with service_context.database.session() as session:
        assert ComputeUnitRepository(session).get(pool.id) == before
        assert (
            ComputeCapacityOperationRepository(session).get(pool.id, acquisition.operation_id)
            == operation
        )


@pytest.mark.parametrize("release_first", [False, True])
def test_registered_reservation_settles_against_durable_purchase_outcome(
    committed_service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
    release_first: bool,
) -> None:
    context = committed_service_context
    _seed_connection(context)
    provider = _PooledProvider()
    redis = real_redis_actors.client()
    reservations = RedisCapacityReservationRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    hooks = _SchedulerHooks()
    compute = ComputeServices.create(
        context,
        provider_resolver=_Resolver(provider, context),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=reservations,
        scheduler_hooks=hooks,
    )
    pool = prepare_unit(compute, desired=0)
    now = datetime.now(UTC)
    request = SchedulerWorkerRequest(
        fairness_account_id="test-account",
        placement=Placement.platform(),
        container_id=str(uuid4()),
        workspace_id=pool.workspace_id,
        stub_id=str(uuid4()),
        cpu_millicores=1_000,
        memory_mib=256,
        timestamp=now,
    )
    with reservations.mutation_lock(pool.capacity_owner_id):
        decision = reservations.reserve(
            capacity_owner_id=pool.capacity_owner_id,
            placement=pool.placement,
            owner_kind=pool.capacity_owner_kind,
            request=request,
            shape=CapacityRequestShape(cpu_millicores=4_000, memory_mib=32 * 1_024),
            registration_timeout=timedelta(minutes=10),
            now=now,
        )
    acquisition = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=decision.reservation.id,
        operation_id=decision.reservation.operation_id,
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32 * 1_024),
    )
    assert compute.capacity.ensure_capacity(acquisition).owns_capacity
    machine_id = str(uuid4())
    _seed_serving_machine(
        context,
        pool,
        hooks,
        machine_id=machine_id,
        instance_id="i-00000000000000000",
        now=now,
    )
    with reservations.mutation_lock(pool.capacity_owner_id):
        reservations.update(
            decision.reservation.model_copy(
                update={
                    "status": CapacityReservationStatus.Registered,
                    "acquisition_created": True,
                    "target_machine_id": machine_id,
                    "target_worker_id": agent_machine_worker_id(machine_id),
                }
            ),
            expected_resource_version=decision.reservation.resource_version,
            now=now,
        )
        reservations.release_allocation(
            request.container_id, expected_reservation_id=decision.reservation.id
        )
        if release_first:
            reservations.release_terminal(decision.reservation.id, now=now)
            compute.capacity.release_acquired_capacity(
                CapacityReleaseRequest(
                    capacity_owner_id=pool.capacity_owner_id,
                    reservation_id=acquisition.reservation_id,
                    operation_id=acquisition.operation_id,
                )
            )
    controller = ComputeUnitCapacityController(
        pool.workspace_id, pool, compute.capacity, workers, 0
    )
    service = CapacityReservationService(reservations, lambda: (controller,))
    service.reconcile([], now=now)
    service.reconcile([], now=now)
    settled = reservations.get(decision.reservation.id)
    assert settled is not None
    assert settled.status is CapacityReservationStatus.Released
    assert not settled.acquisition_created
    with context.database.session() as session:
        repository = ComputeCapacityOperationRepository(session)
        operation = repository.get(pool.capacity_owner_id, acquisition.operation_id)
        assert operation is not None
        if release_first:
            assert operation.status is CapacityOperationStatus.Released
            assert operation.target_machine_id is None
            assert operation.fulfilled_at is None
        else:
            assert operation.status is CapacityOperationStatus.Fulfilled
            assert operation.target_machine_id == machine_id
            assert operation.fulfilled_at is not None
        assert not operation.owns_capacity
        assert repository.list_open_for_owner(pool.capacity_owner_id) == []
        with pytest.raises(ConflictError, match="cannot be reopened"):
            repository.upsert(
                operation.model_copy(update={"status": CapacityOperationStatus.Requested})
            )
    compute.capacity.release_acquired_capacity(
        CapacityReleaseRequest(
            capacity_owner_id=pool.capacity_owner_id,
            reservation_id=acquisition.reservation_id,
            operation_id=acquisition.operation_id,
        )
    )
    assert provider.desired == (0 if release_first else 1)


@pytest.mark.parametrize("replacement", [False, True])
def test_pooled_capacity_acquisition_is_idempotent_and_releases_only_its_unit(
    service_context: ServiceContext,
    replacement: bool,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=0)
    compute.reconciliation.reconcile_pooled_capacity()
    first = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32 * 1_024),
    )
    second = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32 * 1_024),
    )

    requested = compute.capacity.ensure_capacity(first)
    retried = compute.capacity.ensure_capacity(first)
    if replacement:
        with service_context.database.session() as session:
            units = ComputeUnitRepository(session)
            current = units.get(pool.id)
            assert current is not None
            units.upsert(current.model_copy(update={"replacement_machine_id": str(uuid4())}))
        provider.desired += 1
    sibling = compute.capacity.ensure_capacity(second)
    released = compute.capacity.release_acquired_capacity(
        CapacityReleaseRequest(
            capacity_owner_id=first.capacity_owner_id,
            reservation_id=first.reservation_id,
            operation_id=first.operation_id,
        )
    )
    release_retry = compute.capacity.release_acquired_capacity(
        CapacityReleaseRequest(
            capacity_owner_id=first.capacity_owner_id,
            reservation_id=first.reservation_id,
            operation_id=first.operation_id,
        )
    )

    assert requested.status is CapacityAcquisitionStatus.Requested
    assert retried.status is CapacityAcquisitionStatus.ExistingPending
    assert sibling.status is CapacityAcquisitionStatus.Requested
    assert released.status is CapacityAcquisitionStatus.Requested
    assert release_retry.status is CapacityAcquisitionStatus.ExistingPending
    assert provider.desired == 1 + int(replacement)
    durable = compute.providers.get_internal_unit(pool.workspace_id, pool.capacity_owner_id)
    assert durable.desired_machines == 1
    with service_context.database.session() as session:
        repository = ComputeCapacityOperationRepository(session)
        first_operation = repository.get(pool.capacity_owner_id, first.operation_id)
        second_operation = repository.get(pool.capacity_owner_id, second.operation_id)
    assert first_operation is not None and second_operation is not None
    assert [first_operation.status, second_operation.status] == ["released", "requested"]


@pytest.mark.parametrize(
    ("failure_after_operation", "reconciliation_first"),
    [(True, False), (False, False), (True, True)],
)
def test_provider_acquisition_failure_is_scoped_to_its_operation_and_releases_owned_capacity(
    service_context: ServiceContext,
    failure_after_operation: bool,
    reconciliation_first: bool,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider(max_observed_machines=0)
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=0)
    container_id = str(uuid4())
    request = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        demand_container_id=container_id,
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32 * 1_024),
    )
    requested = compute.capacity.ensure_capacity(request)
    assert requested.status is CapacityAcquisitionStatus.Requested
    assert requested.owns_capacity
    with service_context.database.session() as session:
        operation = ComputeCapacityOperationRepository(session).get(
            request.capacity_owner_id, request.operation_id
        )
    assert operation is not None
    provider.last_capacity_failure_at = operation.created_at + timedelta(
        seconds=1 if failure_after_operation else -1
    )
    provider.last_capacity_failure_code = CapacityFailureCode.CapacityUnavailable

    if reconciliation_first:
        compute.reconciliation.reconcile_pooled_capacity()
        # Once the provider stops launches, later observations omit the failure.
        provider.last_capacity_failure_at = None

    observed = compute.capacity.ensure_capacity(request)
    retained = compute.providers.get_internal_unit(pool.workspace_id, pool.capacity_owner_id)
    if not failure_after_operation:
        assert observed.status is CapacityAcquisitionStatus.ExistingPending
        assert observed.owns_capacity
        assert retained.provider_state.degraded_reason is None
        assert provider.desired == 1
        return

    assert observed.status is CapacityAcquisitionStatus.Rejected
    assert observed.owns_capacity
    assert observed.failure_code is CapacityFailureCode.CapacityUnavailable
    assert observed.reason == "provider has no matching capacity available"
    with service_context.database.session() as session:
        failed_operation = ComputeCapacityOperationRepository(session).get(
            request.capacity_owner_id, request.operation_id
        )
    assert failed_operation is not None
    assert failed_operation.last_error == observed.reason
    assert failed_operation.failure_code is CapacityFailureCode.CapacityUnavailable
    assert retained.provider_state.degraded_reason == "provider_acquisition_rejected"
    assert compute.capacity.ensure_capacity(request).status is CapacityAcquisitionStatus.Rejected
    release = CapacityReleaseRequest(
        capacity_owner_id=request.capacity_owner_id,
        reservation_id=request.reservation_id,
        operation_id=request.operation_id,
    )
    assert (
        compute.capacity.release_acquired_capacity(release).status
        is CapacityAcquisitionStatus.Requested
    )
    assert (
        compute.capacity.release_acquired_capacity(release).status
        is CapacityAcquisitionStatus.ExistingPending
    )
    assert provider.desired == 0
    with service_context.database.session() as session:
        released = ComputeCapacityOperationRepository(session).get(
            request.capacity_owner_id, request.operation_id
        )
    assert released is not None
    assert released.status == "released"
    with service_context.database.session() as session:
        assert ComputeCapacityOperationRepository(session).latest_failures_for_containers(
            [container_id]
        ) == {container_id: CapacityFailureCode.CapacityUnavailable}


def test_pooled_capacity_does_not_sell_one_pending_unit_twice(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=0)
    compute.reconciliation.reconcile_pooled_capacity()
    first = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32 * 1_024),
    )
    second = first.model_copy(update={"reservation_id": str(uuid4()), "operation_id": str(uuid4())})
    concurrent_results: list[CapacityAcquisitionStatus] = []

    def acquire_while_provider_is_stale(_request: ProviderUnitRequest) -> None:
        provider.before_capacity = None
        concurrent_results.append(compute.capacity.ensure_capacity(second).status)

    provider.before_capacity = acquire_while_provider_is_stale

    requested = compute.capacity.ensure_capacity(first)

    assert requested.status is CapacityAcquisitionStatus.Requested
    assert concurrent_results == [CapacityAcquisitionStatus.ExistingPending]
    assert provider.desired == 1
    with service_context.database.session() as session:
        repository = ComputeCapacityOperationRepository(session)
        first_operation = repository.get(pool.capacity_owner_id, first.operation_id)
        second_operation = repository.get(pool.capacity_owner_id, second.operation_id)
    assert first_operation is not None and second_operation is not None
    assert first_operation.owns_capacity
    assert not second_operation.owns_capacity


def test_disconnecting_connection_rejects_a_previously_selected_purchase(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    with service_context.database.session() as session:
        stored_connection = AwsAccountConnectionRepository(session).get(_CONNECTION_ID)
        workspace_id = service_context.default_workspace_id(session)
    assert stored_connection is not None
    connection = stored_connection
    resolver = WorkspaceComputeProviderResolver(
        connections=lambda _: (connection,),
        capacity_workspace=lambda _: workspace_id,
        binaries_by_region={
            "us-east-1": AwsManagedPoolBinaries(
                agent_version="0.1.0",
                agent_sha256="a" * 64,
                cpu_ami_id="ami-0123456789abcdef0",
            )
        },
        client_provider=Boto3AwsManagedPoolClientProvider.from_default_chain(),
    )
    provider = resolver.resolve(workspace_id, f"aws:{_CONNECTION_ID}")
    approved = next(item for item in AWS_ALLOWED_OFFERS if item.region == "us-east-1")
    offer = _offer().model_copy(
        update={
            "region": approved.region,
            "instance_type": approved.instance_type,
            "preemptible": approved.preemptible,
        }
    )
    connection = connection.model_copy(
        update={"phase": AwsAccountConnectionPhase.DisconnectDraining}
    )
    compute = ComputeServices.create(service_context, provider_resolver=resolver)

    with pytest.raises(ConflictError, match="no longer accepts"):
        compute.provisioning.prepare_pooled_offer(
            provider=provider,
            offer=offer,
            requirements=ComputeResourceRequirements(cpu_millicores=1000, memory_mb=1024),
        )

    with service_context.database.session() as session:
        assert ComputeUnitRepository(session).list_for_provider_connection(_CONNECTION_ID) == []


def test_degraded_pool_refuses_acquisition_and_placement_does_not_clear_it(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=0)
    compute.reconciliation.reconcile_pooled_capacity()
    with service_context.database.session() as session:
        pools = ComputeUnitRepository(session)
        stored = pools.get(pool.id)
        assert stored is not None
        pools.upsert(
            stored.model_copy(
                update={
                    "provider_state": stored.provider_state.model_copy(
                        update={"degraded_reason": "bootstrap_launch_attempts_exhausted"}
                    )
                }
            )
        )
    capacity_calls_before = list(provider.capacity_calls)

    refused = compute.capacity.ensure_capacity(
        CapacityAcquisitionRequest(
            capacity_owner_id=pool.capacity_owner_id,
            reservation_id=str(uuid4()),
            operation_id=str(uuid4()),
            shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32 * 1_024),
        )
    )

    assert refused.status is CapacityAcquisitionStatus.TemporarilyUnavailable
    assert refused.reason == "bootstrap_launch_attempts_exhausted"
    assert provider.capacity_calls == capacity_calls_before

    with pytest.raises(UpstreamUnavailableError, match="cooling down"):
        prepare_unit(compute, desired=1)
    with service_context.database.session() as session:
        after = ComputeUnitRepository(session).get(pool.id)
    assert after is not None
    assert after.desired_machines == 0
    assert after.provider_state.degraded_reason == "bootstrap_launch_attempts_exhausted"
