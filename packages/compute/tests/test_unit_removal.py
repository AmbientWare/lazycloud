from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from compute.capacity_errors import CapacityReservationConflictError
from compute.service import ComputeServices
from database.context import ServiceContext
from database.repositories.aws_connections import AwsAccountConnectionRepository
from database.repositories.capacity_recovery import CapacityRecoveryRepository
from database.repositories.compute import (
    ComputeJoinCredentialRepository,
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import MachineRepository, WorkerRepository
from database.tables.capacity_recovery import CapacityRecoveryTable
from packages.compute.tests.pooled_fixtures import (
    _allow_scale,
    _bind_test_machine,
    _bootstrap,
    _EmptyAccountProvider,
    _MutationLeases,
    _open_record,
    _PooledProvider,
    _Resolver,
    _SchedulerHooks,
    _seed_connection,
    pooled_service,
    prepare_unit,
)
from scheduler.capacity_reservations import RedisCapacityReservationRepository
from shared.compute_enrollment import (
    ComputeCredentialStatus,
    ComputeMachineEnrollmentStatus,
    MachineReadinessPhase,
    agent_machine_worker_id,
)
from shared.compute_fleet import Machine, MachineLifecycle, ResourceStatus, Worker
from shared.compute_policy import ComputeResourceRequirements, ComputeUnitPhase, ComputeUnitRecord
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.identity import WorkspaceStatus
from tests.real_redis import RealRedisActors


@pytest.mark.parametrize("platform_fleet", [True, False])
def test_provider_disable_preserves_cleanup_and_customer_cloud(
    service_context: ServiceContext, platform_fleet: bool
) -> None:
    _seed_connection(service_context, platform_fleet=platform_fleet)
    supplier = _PooledProvider()
    resolver = _Resolver(supplier, service_context)
    compute = ComputeServices.create(
        service_context,
        provider_resolver=resolver,
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_unit_capacity(pool.id)
    resolver.purchases_enabled = False
    if platform_fleet:
        supplier.catalog_failure = RuntimeError("disabled provider catalog must not be queried")
        with pytest.raises(ConflictError, match="purchases are disabled"):
            compute.scaling.scale_internal_unit(
                pool.workspace_id, pool.id, 2, before_mutation=_allow_scale
            )
        compute.reconciliation.reconcile_unit_capacity(pool.id)
        assert supplier.desired == 1
        assert not supplier.ensure_calls[-1].purchases_enabled
    else:
        compute.scaling.scale_internal_unit(
            pool.workspace_id, pool.id, 2, before_mutation=_allow_scale
        )
        assert supplier.desired == 2
    compute.scaling.scale_internal_unit(pool.workspace_id, pool.id, 0, before_mutation=_allow_scale)
    assert supplier.desired == 0
    resolver.purchases_enabled = True
    supplier.catalog_failure = None
    compute.scaling.scale_internal_unit(pool.workspace_id, pool.id, 1, before_mutation=_allow_scale)
    assert supplier.desired == 1


def test_connection_drain_deletes_hidden_capacity_idempotently(
    committed_service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
) -> None:
    _seed_connection(committed_service_context)
    provider = _PooledProvider()
    mutations = RedisCapacityReservationRepository(real_redis_actors.client())
    compute = ComputeServices.create(
        committed_service_context,
        provider_resolver=_Resolver(provider, committed_service_context),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=mutations,
    )
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_pooled_capacity()

    with committed_service_context.database.session() as session:
        units = ComputeUnitRepository(session)
        stored = units.get(pool.id)
        assert stored is not None
        units.upsert(stored.model_copy(update={"initial_machines": 1, "min_machines": 1}))

    drained = compute.removal.request_connection_drain(
        pool.provider_connection_id or "",
        workspace_ids=[pool.workspace_id],
    )
    repeated = compute.removal.request_connection_drain(
        pool.provider_connection_id or "",
        workspace_ids=[pool.workspace_id],
    )
    reconciled = compute.reconciliation.reconcile_pooled_capacity()

    assert drained.total_pools == 1
    assert drained.remaining_pools == 0
    assert repeated == drained
    assert reconciled == []
    assert len(provider.ensure_calls) == 1
    assert len(provider.delete_calls) == 1


def test_pool_delete_takes_the_provider_pool_with_it_or_keeps_the_pool_owned(
    service_context: ServiceContext,
) -> None:

    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=0)
    compute.reconciliation.reconcile_pooled_capacity()
    provider.catalog_failure = RuntimeError("supplier catalog unavailable")
    provider.delete_failure = RuntimeError("provider pool deletion failed")

    with pytest.raises(UpstreamUnavailableError, match="provider pool deletion failed"):
        compute.removal.delete_unit(pool.capacity_owner_id, workspace=pool.workspace_id)

    with service_context.database.session() as session:
        retained = ComputeUnitRepository(session).get(pool.id)
    assert retained is not None
    assert provider.delete_calls == []

    recovery_id = str(uuid4())
    now = datetime.now(UTC)
    with service_context.database.session() as session:
        session.add(
            CapacityRecoveryTable(
                id=recovery_id,
                workspace_id=pool.workspace_id,
                source_unit_id=pool.id,
                source_machine_id=str(uuid4()),
                target_unit_id=pool.id,
                operation_id=str(uuid4()),
                observed_at=now,
                next_action_at=now,
                completed_at=now,
                reason="replacement accepted work",
            )
        )

    provider.delete_failure = None
    compute.removal.delete_unit(pool.capacity_owner_id, workspace=pool.workspace_id)

    with service_context.database.session() as session:
        deleted = ComputeUnitRepository(session).get(pool.id)
        recovery = CapacityRecoveryRepository(session).get(recovery_id)
    assert [request.unit_id for request in provider.delete_calls] == [pool.id]
    assert deleted is not None and deleted.phase is ComputeUnitPhase.Deleted
    assert recovery is not None and recovery.completed_at == now

    compute.removal.delete_unit(pool.capacity_owner_id, workspace=pool.workspace_id)


def test_connection_drain_terminalizes_provider_nodes_and_preserves_history(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    hooks = _SchedulerHooks()
    compute = pooled_service(service_context, provider, scheduler_hooks=hooks)
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_pooled_capacity()
    first = compute.removal.request_connection_drain(
        pool.provider_connection_id or "",
        workspace_ids=[pool.workspace_id],
    )
    machine_id = "33333333-3333-4333-8333-333333333333"
    worker_id = agent_machine_worker_id(machine_id)
    now = datetime.now(UTC)
    with service_context.database.session() as session:
        connection = AwsAccountConnectionRepository(session).get(pool.provider_connection_id or "")
        assert connection is not None
        owner_user_id = connection.user_id
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
        WorkerRepository(session).upsert(
            Worker(
                id=worker_id,
                machine_id=machine_id,
                placement=pool.placement,
                status=ResourceStatus.Running,
            ),
            workspace_id=pool.workspace_id,
        )
        credential = ComputeJoinCredentialRepository(session).create(
            token_hash="a" * 64,
            user_id=owner_user_id,
            workspace_id=pool.workspace_id,
            capacity_owner_id=pool.capacity_owner_id,
            placement=pool.placement,
            created_by_token_id=None,
            max_uses=1,
            expires_at=now + timedelta(minutes=2),
        )
        enrollment = ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                user_id=owner_user_id,
                workspace_id=pool.workspace_id,
                capacity_owner_id=pool.capacity_owner_id,
                placement=pool.placement,
                machine_id=machine_id,
                machine_fingerprint_hash="b" * 64,
                join_credential_id=credential.id,
                credential_hash="c" * 64,
                status=ComputeMachineEnrollmentStatus.Active,
                preflight_passed=True,
                heartbeat_confirmed=True,
                schedulable=True,
                readiness_phase=MachineReadinessPhase.Ready,
                last_join_at=now,
                last_heartbeat_at=now,
            )
        )
        bound = _bind_test_machine(session, pool.id, "i-00000000000000000", machine_id)
        assert bound is not None

    repeated = compute.removal.request_connection_drain(
        pool.provider_connection_id or "",
        workspace_ids=[pool.workspace_id],
    )

    assert first.remaining_pools == 0
    assert repeated == first
    assert len(provider.delete_calls) == 1
    assert compute.units.list_machines(workspace=pool.workspace_id) == []
    with service_context.database.session() as session:
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            pool.workspace_id,
            machine_id,
            placement=pool.placement,
        )
        machine = MachineRepository(session).get(machine_id, workspace_id=pool.workspace_id)
        worker = WorkerRepository(session).get(worker_id, workspace_id=pool.workspace_id)
        durable_credential = ComputeJoinCredentialRepository(session).get(credential.id)
        assert enrollment is not None
    assert enrollment.status is ComputeMachineEnrollmentStatus.Deleted
    assert enrollment.schedulable is False
    assert enrollment.heartbeat_confirmed is False
    assert enrollment.readiness_phase is MachineReadinessPhase.Revoked
    assert machine is not None and machine.lifecycle is MachineLifecycle.Deleted
    assert worker is not None and worker.status is ResourceStatus.Deleted
    assert durable_credential is not None
    assert durable_credential.status is ComputeCredentialStatus.Revoked
    assert {item[1] for item in hooks.retired} == {machine_id}
    assert set(hooks.revoked_join_tokens) == {credential.token_hash}


def test_workspace_deletion_terminates_a_live_node_while_the_workspace_is_deleting(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_pooled_capacity()
    record = _open_record(service_context, pool.id)
    assert record is not None and record.machine_id is not None
    with service_context.database.session() as session:
        workspaces = WorkspaceRepository(session)
        workspace = workspaces.get(pool.workspace_id)
        assert workspace is not None
        workspaces.upsert(workspace.model_copy(update={"status": WorkspaceStatus.Deleting}))

    with service_context.database.session() as session:
        compute.providers.machines._terminate_provider_record(
            session,
            record,
            clients={},
            reason="workspace_deleted",
            message="workspace deletion terminated managed compute pool",
            deleting_workspace_id=pool.workspace_id,
        )
    with service_context.database.session() as session:
        machine = MachineRepository(session).get_across_workspaces(record.machine_id)
        terminated = ComputeProviderInstanceRepository(session).get_by_machine(record.machine_id)
    assert machine is not None and machine.lifecycle is MachineLifecycle.Terminating
    assert terminated is not None and terminated.status == "terminating"


@pytest.mark.parametrize(
    ("policy_change", "platform_fleet", "retired"),
    [
        ("disabled", True, True),
        ("removed_offer", True, True),
        ("root_disk", True, True),
        ("unchanged", True, True),
        ("disabled", False, False),
    ],
)
def test_empty_pool_retirement_preserves_customer_owned_pools(
    service_context: ServiceContext, policy_change: str, platform_fleet: bool, retired: bool
) -> None:
    _seed_connection(service_context, platform_fleet=platform_fleet)
    provider = (
        _EmptyAccountProvider()
        if policy_change == "disabled" and platform_fleet
        else _PooledProvider()
    )
    resolver = _Resolver(provider, service_context)
    compute = ComputeServices.create(
        service_context,
        provider_resolver=resolver,
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = prepare_unit(compute, desired=0)
    if policy_change == "disabled":
        resolver.purchases_enabled = False
    elif policy_change == "removed_offer":
        resolver.allowed_offers = ()
    elif policy_change == "root_disk":
        resolver.root_volume_gib = 300
    provider.catalog_failure = RuntimeError("temporary quote outage")

    compute.reconciliation.reconcile_unit_capacity(
        pool.id, now=pool.created_at + timedelta(seconds=pool.idle_drain_timeout_seconds + 1)
    )

    with service_context.database.session() as session:
        stored = ComputeUnitRepository(session).get(pool.id)
    assert stored is not None
    assert (stored.phase is ComputeUnitPhase.Deleted) is retired
    assert stored.created_at == pool.created_at
    assert stored.capacity_owner_id == pool.capacity_owner_id
    assert (pool.id in {unit.id for unit in compute.units.list_units()}) is not retired


def test_failed_empty_market_retires_and_only_new_demand_reopens_it(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context, platform_fleet=True)
    provider = _PooledProvider()
    compute = pooled_service(service_context, provider)
    requirements = ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024)
    pool = compute.provisioning.prepare_pooled_capacity(
        workspace="default",
        requirements=requirements,
        region="us-east-1",
        desired_machines=0,
        root_volume_gib=200,
    )
    failed_at = datetime.now(UTC) - timedelta(seconds=pool.registration_timeout_seconds + 1)
    with service_context.database.session() as session:
        ComputeUnitRepository(session).upsert(
            pool.model_copy(
                update={
                    "phase": ComputeUnitPhase.Degraded,
                    "provider_state": pool.provider_state.model_copy(
                        update={
                            "degraded_reason": "provider_acquisition_rejected",
                            "degraded_at": failed_at,
                        }
                    ),
                }
            )
        )
    now = pool.created_at + timedelta(seconds=pool.idle_drain_timeout_seconds + 1)
    retired = compute.reconciliation.reconcile_unit_capacity(pool.id, now=now)
    assert retired is not None and retired.phase is ComputeUnitPhase.Deleted
    assert retired.provider_state.degraded_at == failed_at
    assert all(unit.id != retired.id for unit in compute.units.list_units_across_workspaces())
    with service_context.database.session() as session:
        assert ComputeUnitRepository(session).get(retired.id) is not None
    assert (
        compute.reconciliation.reconcile_unit_capacity(pool.id, now=now + timedelta(hours=1))
        is None
    )
    assert provider.desired == 0
    revived = compute.provisioning.prepare_pooled_capacity(
        workspace="default",
        requirements=requirements,
        region="us-east-1",
        desired_machines=1,
        root_volume_gib=200,
    )
    assert revived.generation == retired.generation + 1
    assert revived.provider_state.degraded_reason is None
    assert revived.desired_machines == 1
    assert revived.id in {unit.id for unit in compute.units.list_units_across_workspaces()}


def test_retirement_cannot_revive_until_provider_deletion_finishes(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context, platform_fleet=True)
    provider = _PooledProvider()
    resolver = _Resolver(provider, service_context)
    leases = _MutationLeases()
    compute = ComputeServices.create(
        service_context,
        provider_resolver=resolver,
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=leases,
    )

    def prepare() -> ComputeUnitRecord:
        return prepare_unit(compute, desired=0)

    pool = prepare()
    resolver.purchases_enabled = False
    leases.reserved_owners.add(pool.capacity_owner_id)
    protected = compute.reconciliation.reconcile_unit_capacity(pool.id)
    assert protected is not None and protected.phase is ComputeUnitPhase.Ready
    leases.reserved_owners.clear()
    provider.delete_failure = RuntimeError("provider deletion is unavailable")
    compute.reconciliation.reconcile_unit_capacity(pool.id)
    with service_context.database.session() as session:
        retiring = ComputeUnitRepository(session).get(pool.id)
    assert retiring is not None and retiring.phase is ComputeUnitPhase.Deleting

    resolver.purchases_enabled = True
    with pytest.raises(CapacityReservationConflictError, match="finishing provider resource"):
        prepare()
    provider.delete_failure = None
    deleted = compute.reconciliation.reconcile_unit_capacity(pool.id)
    assert deleted is not None and deleted.phase is ComputeUnitPhase.Deleted
    revived = prepare()
    assert revived.id == pool.id
    assert revived.phase is ComputeUnitPhase.Provisioning
    assert revived.generation == retiring.generation + 1


def test_obsolete_pool_retains_history_until_provider_storage_is_destroyed(
    service_context: ServiceContext,
) -> None:
    _seed_connection(service_context, platform_fleet=True)
    provider = _PooledProvider()
    resolver = _Resolver(provider, service_context)
    compute = ComputeServices.create(
        service_context,
        provider_resolver=resolver,
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = prepare_unit(compute, desired=1)
    compute.reconciliation.reconcile_unit_capacity(pool.id)
    with service_context.database.session() as session:
        instance = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)[0]
    assert instance.instance_id is not None
    provider.lingering_storage.add(instance.instance_id)
    compute.scaling.scale_internal_unit(
        pool.workspace_id, pool.capacity_owner_id, 0, before_mutation=lambda _unit: None
    )
    resolver.purchases_enabled = False

    protected = compute.reconciliation.reconcile_unit_capacity(pool.id)
    assert protected is not None and protected.phase is ComputeUnitPhase.Updating
    with service_context.database.session() as session:
        records = ComputeProviderInstanceRepository(session)
        waiting = records.list_for_pool(pool.id)[0]
        records.upsert(waiting.model_copy(update={"status": "failed"}))
    provider.lingering_storage.clear()
    retired = compute.reconciliation.reconcile_unit_capacity(pool.id)
    assert retired is not None and retired.phase is ComputeUnitPhase.Deleted
    with service_context.database.session() as session:
        records = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        assert len(records) == 1 and records[0].id == instance.id
        assert records[0].status == "deleted"
        assert records[0].provider_storage_destroyed_at is not None


def test_an_unbuilt_pool_is_not_deleted_by_the_account_it_has_not_been_built_in(
    service_context: ServiceContext,
) -> None:

    _seed_connection(service_context)
    compute = ComputeServices.create(
        service_context,
        provider_resolver=_Resolver(_EmptyAccountProvider(), service_context),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    unit = compute.provisioning.reconcile_aws_default_capacity(
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

    described, _ = compute.scaling.describe_internal_unit(unit.workspace_id, unit.capacity_owner_id)

    assert described.phase is ComputeUnitPhase.Provisioning
    assert described.desired_machines == 1
