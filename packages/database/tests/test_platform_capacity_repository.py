from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from database.repositories.aws_connections import AwsAccountConnectionRepository
from database.repositories.capacity_recovery import CapacityRecoveryRepository
from database.repositories.compute import (
    ComputeCapacityOperationRecord,
    ComputeCapacityOperationRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.identity import UserRepository, WorkspaceRepository
from database.repositories.orchestration import MachineRepository
from database.tables.capacity_recovery import CapacityRecoveryTable
from database.tables.compute import ComputeCapacityOperationTable, ComputeUnitTable
from identity.platform import PlatformNamespaceService
from shared.aws_connections import AwsAccountConnection, AwsAccountConnectionPhase
from shared.capacity import (
    CapacityAcquisitionShape,
    CapacityOperationStatus,
    CapacityOwnerKind,
    CapacityOwnerSource,
)
from shared.compute_fleet import Machine
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitPhase,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    UnitName,
)
from shared.compute_reconciliation import ComputeReconciliationKind
from shared.placement import Placement
from shared.timestamps import utc_now
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from database import DatabaseClient


def test_terminal_capacity_handoff_rejects_a_stale_writer(database: DatabaseClient) -> None:
    with database.session() as session:
        workspace = PlatformNamespaceService(database).initialize()
        unit = ComputeUnitRepository(session).upsert(_platform_unit(workspace.id, "aws"))
        operation_id = str(uuid4())
        operation = ComputeCapacityOperationRepository(session).upsert(
            ComputeCapacityOperationRecord(
                shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32_768),
                id=operation_id,
                workspace_id=workspace.id,
                pool_id=unit.id,
                capacity_owner_id=unit.id,
                reservation_id=str(uuid4()),
                operation_id=operation_id,
                desired_unit=1,
                status=CapacityOperationStatus.Fulfilled,
                target_machine_id=str(uuid4()),
                fulfilled_at=utc_now(),
                demand_container_id=str(uuid4()),
                owns_capacity=False,
            )
        )
    with database.session() as session:
        with (
            pytest.raises(IntegrityError, match="terminal capacity ownership"),
            session.begin_nested(),
        ):
            session.execute(
                update(ComputeCapacityOperationTable)
                .where(
                    ComputeCapacityOperationTable.id == operation_id,
                )
                .values(status="released")
            )
        persisted = ComputeCapacityOperationRepository(session).get(unit.id, operation_id)
        assert persisted is not None and persisted.status is CapacityOperationStatus.Fulfilled
        assert persisted.fulfilled_at == operation.fulfilled_at
        assert persisted.demand_container_id == operation.demand_container_id
        assert not persisted.owns_capacity


def test_recovery_cleanup_preserves_pending_and_other_units(database: DatabaseClient) -> None:
    workspace = PlatformNamespaceService(database).initialize()
    now = utc_now()
    with database.session() as session:
        units = ComputeUnitRepository(session)
        retired = units.upsert(_platform_unit(workspace.id, "aws"))
        sibling = units.upsert(_platform_unit(workspace.id, "aws"))
        completed_id, pending_id, sibling_id, shared_id = (str(uuid4()) for _ in range(4))
        for identity, source, target, completed_at in (
            (completed_id, retired.id, retired.id, now),
            (pending_id, retired.id, retired.id, None),
            (sibling_id, sibling.id, sibling.id, now),
            (shared_id, retired.id, sibling.id, now),
        ):
            session.add(
                CapacityRecoveryTable(
                    id=identity,
                    workspace_id=workspace.id,
                    source_unit_id=source,
                    source_machine_id=str(uuid4()),
                    target_unit_id=target,
                    operation_id=str(uuid4()),
                    observed_at=now,
                    next_action_at=now,
                    completed_at=completed_at,
                )
            )
    with database.session() as session:
        recovery = CapacityRecoveryRepository(session)
        recovery.delete_completed_for_units([retired.id])
        assert recovery.get(completed_id) is None
        assert recovery.get(pending_id) is not None
        assert recovery.get(sibling_id) is not None
        assert recovery.get(shared_id) is not None


def _platform_unit(workspace_id: str, provider: str) -> ComputeUnitRecord:
    unit_id = str(uuid4())
    return ComputeUnitRecord(
        id=unit_id,
        workspace_id=workspace_id,
        name=UnitName(unit_id),
        placement=Placement.platform(),
        provider=provider,
        provider_ref=f"{provider}:fleet",
        platform_fleet=True,
        capacity_mode=ComputeCapacityMode.Pooled,
        visibility=ComputeUnitVisibility.Internal,
        capacity_owner_id=unit_id,
        capacity_owner_kind=CapacityOwnerKind.PooledProvider,
        capacity_owner_source=CapacityOwnerSource.Provider,
        region="us-east-1",
        offer_id=unit_id,
        capability_key=unit_id,
        max_machines=100,
    )


def test_capacity_batches_share_work_and_keep_empty_pool_audits_progressing(
    database: DatabaseClient,
) -> None:
    with database.session() as session:
        workspace = PlatformNamespaceService(database).initialize()
        repository = ComputeUnitRepository(session)
        active = {
            repository.upsert(
                _platform_unit(workspace.id, "aws").model_copy(update={"desired_machines": 1})
            ).id
            for _ in range(4)
        }
        inactive = {repository.upsert(_platform_unit(workspace.id, "aws")).id for _ in range(2)}
        updated_at = {
            unit.id: unit.updated_at for unit in session.scalars(select(ComputeUnitTable))
        }
    claimed = Barrier(2)
    now = utc_now()

    def select_batch() -> set[str]:
        with database.session() as session:
            batch = ComputeUnitRepository(session).claim_reconciliation_batch(
                ComputeReconciliationKind.Provider, now=now, limit=3
            )
            claimed.wait(timeout=5)
            return {unit.id for unit in batch}

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(select_batch)
        second = executor.submit(select_batch)
        batches = [first.result(), second.result()]
    assert batches[0].isdisjoint(batches[1])
    assert batches[0] | batches[1] == active | inactive
    for batch in batches:
        assert len(batch & active) == 2
        assert len(batch & inactive) == 1
    with database.session() as session:
        assert {
            unit.id: unit.updated_at for unit in session.scalars(select(ComputeUnitTable))
        } == updated_at


def test_fleet_capacity_counts_commitments_and_retiring_nodes_once(
    database: DatabaseClient,
) -> None:
    with database.session() as session:
        workspace = PlatformNamespaceService(database).initialize()
        customer = WorkspaceRepository(session).create(name="customer")
        repository = ComputeUnitRepository(session)
        old_machine_id = str(uuid4())
        MachineRepository(session).upsert(
            Machine(id=old_machine_id, provider="aws"), workspace_id=workspace.id
        )
        reserved = repository.upsert(
            _platform_unit(workspace.id, "aws").model_copy(
                update={
                    "desired_machines": 2,
                    "observed_machines": 2,
                    "replacement_machine_id": old_machine_id,
                    "worker_preemptible": True,
                }
            )
        )
        draining = repository.upsert(
            _platform_unit(workspace.id, "removed-provider").model_copy(
                update={"desired_machines": 1}
            )
        )
        observed = repository.upsert(
            _platform_unit(workspace.id, "aws").model_copy(update={"observed_machines": 4})
        )
        gpu = repository.upsert(
            _platform_unit(workspace.id, "aws").model_copy(
                update={"desired_machines": 2, "worker_gpu_type": "a100", "worker_gpu_count": 8}
            )
        )
        now = utc_now()
        connection = AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=str(uuid4()),
                user_id=UserRepository(session).create(display_name="customer").id,
                account_id="123456789012",
                external_id=uuid4().hex,
                phase=AwsAccountConnectionPhase.AwaitingAuthorization,
                created_at=now,
                updated_at=now,
            )
        )
        repository.upsert(
            _platform_unit(customer.id, "aws").model_copy(
                update={
                    "platform_fleet": False,
                    "provider_connection_id": connection.id,
                    "desired_machines": 90,
                }
            )
        )
        repository.upsert(
            ComputeUnitRecord(
                id=str(uuid4()),
                workspace_id=customer.id,
                name=UnitName("public"),
                placement=Placement.platform(),
                desired_machines=90,
                max_machines=90,
            )
        )
        instances = ComputeProviderInstanceRepository(session)
        for unit, statuses in (
            (reserved, ("active", "terminating")),
            (draining, ("terminating", "terminating", "deleted", "failed")),
        ):
            for status in statuses:
                instances.upsert(
                    ComputeProviderInstanceRecord(
                        id=str(uuid4()),
                        pool_id=unit.id,
                        provider=unit.provider,
                        offer_id=unit.offer_id,
                        instance_id=str(uuid4()),
                        machine_id=(
                            old_machine_id
                            if unit.id == reserved.id and status == "terminating"
                            else None
                        ),
                        status=status,
                        source="pooled",
                    )
                )
        assert repository.platform_capacity_usage(gpu=False) == 10
        assert repository.platform_capacity_usage(gpu=False, excluding_unit_id=reserved.id) == 7
        assert repository.platform_capacity_usage(gpu=True) == 2
        assert {unit.id for unit in repository.list_platform_internal()} == {
            reserved.id,
            draining.id,
            observed.id,
            gpu.id,
        }
        assert [
            unit.id for unit in repository.list_platform_internal(preemptible=True, gpu=False)
        ] == [reserved.id]


def test_stopped_capacity_holds_its_budget_through_resume_and_retirement(
    database: DatabaseClient,
) -> None:
    workspace = PlatformNamespaceService(database).initialize()
    with database.session() as session:
        units = ComputeUnitRepository(session)
        unit = units.upsert(
            _platform_unit(workspace.id, "aws").model_copy(
                update={
                    "desired_machines": 0,
                    "stopped_machines": 2,
                    "max_machines": 2,
                }
            )
        )
        instances = ComputeProviderInstanceRepository(session)
        for status in ("stopped", "preparing"):
            instances.upsert(
                ComputeProviderInstanceRecord(
                    id=str(uuid4()),
                    pool_id=unit.id,
                    provider=unit.provider,
                    offer_id=unit.offer_id,
                    instance_id=str(uuid4()),
                    status=status,
                    source="pooled",
                )
            )
        assert units.platform_capacity_usage(gpu=False) == 2
        sizing = units.sizing_for_owner(unit.id)
        assert sizing is not None and sizing.desired_machines == 0
        assert units.prepared_capacity_owner_ids() == {unit.id}
        resumed = units.update_capacity(
            unit.id,
            expected_generation=unit.generation,
            desired_machines=1,
            max_machines=2,
            observed_machines=0,
            phase=ComputeUnitPhase.Updating,
            provider_state=unit.provider_state,
        )
        assert resumed is not None
        assert resumed.stopped_machines == 1
        assert units.platform_capacity_usage(gpu=False) == 2
        units.upsert(
            resumed.model_copy(
                update={
                    "stopped_machines": 0,
                    "retiring_stopped_machines": 1,
                }
            )
        )
        assert units.platform_capacity_usage(gpu=False) == 2
        for instance in instances.list_for_pool(unit.id):
            instances.upsert(instance.model_copy(update={"status": "deleted"}))
        assert not units.prepared_capacity_owner_ids()
        assert units.platform_capacity_usage(gpu=False) == 2
        units.upsert(resumed.model_copy(update={"stopped_machines": 0}))
        assert units.platform_capacity_usage(gpu=False) == 1


def test_fleet_capacity_lock_serializes_purchases_across_units(
    database: DatabaseClient,
) -> None:
    with database.session() as session:
        workspace_id = PlatformNamespaceService(database).initialize().id
        units = [_platform_unit(workspace_id, "aws") for _ in range(2)]
    ready = Barrier(2)

    def purchase(unit: ComputeUnitRecord) -> bool:
        with database.session() as session:
            repository = ComputeUnitRepository(session)
            ready.wait(timeout=5)
            repository.lock_platform_capacity()
            if repository.platform_capacity_usage(gpu=False) >= 1:
                return False
            repository.upsert(unit.model_copy(update={"desired_machines": 1}))
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(purchase, units)) == [False, True]
    with database.session() as session:
        assert ComputeUnitRepository(session).platform_capacity_usage(gpu=False) == 1
