from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from database.repositories.compute import (
    AwsAccountConnectionRepository,
    ComputeCapacityOperationRecord,
    ComputeCapacityOperationRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.identity import UserRepository, WorkspaceRepository
from database.repositories.orchestration import MachineRepository
from database.tables.compute import ComputeCapacityOperationTable, ComputeUnitTable
from shared.aws_connections import AwsAccountConnection, AwsAccountConnectionPhase
from shared.capacity import CapacityOperationStatus, CapacityOwnerKind, CapacityOwnerSource
from shared.compute_fleet import Machine
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    MachinePool,
    UnitName,
)
from shared.compute_reconciliation import ComputeReconciliationKind
from shared.timestamps import utc_now
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from database import DatabaseClient


def test_terminal_capacity_handoff_rejects_a_stale_writer(database: DatabaseClient) -> None:
    with database.session() as session:
        workspace = WorkspaceRepository(session).create(name="handoff-fencing")
        unit = ComputeUnitRepository(session).upsert(_platform_unit(workspace.id, "aws"))
        operation_id = str(uuid4())
        operation = ComputeCapacityOperationRepository(session).upsert(
            ComputeCapacityOperationRecord(
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


def _platform_unit(workspace_id: str, provider: str) -> ComputeUnitRecord:
    unit_id = str(uuid4())
    return ComputeUnitRecord(
        id=unit_id,
        workspace_id=workspace_id,
        name=UnitName(unit_id),
        pool=MachinePool("lazycloud"),
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
        workspace = WorkspaceRepository(session).create(name="reconciliation")
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
        workspace = WorkspaceRepository(session).create(name="platform")
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
            _platform_unit(workspace.id, "hetzner").model_copy(update={"observed_machines": 4})
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
                workspace_id=workspace.id,
                name=UnitName("public"),
                pool=MachinePool("lazycloud"),
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


def test_fleet_capacity_lock_serializes_purchases_across_workspaces(
    database: DatabaseClient,
) -> None:
    with database.session() as session:
        units = [
            _platform_unit(WorkspaceRepository(session).create(name=provider).id, provider)
            for provider in ("aws", "hetzner")
        ]
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
