from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from database.context import ServiceContext
from database.repositories.compute import (
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.orchestration import MachineRepository
from database.tables.compute import ComputeProviderInstanceTable
from shared.compute_fleet import Machine
from shared.compute_policy import (
    ComputeUnitRecord,
    MachinePool,
    UnitName,
)
from shared.timestamps import utc_now
from sqlalchemy import update


def test_provider_instance_machine_binding_is_idempotent_and_fenced(
    service_context: ServiceContext,
) -> None:
    with service_context.database.session() as session:
        workspace_id = service_context.default_workspace_id(session)
        pool = ComputeUnitRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            name=UnitName("provider-binding"),
            pool=MachinePool("provider-binding"),
        )
        ComputeUnitRepository(session).upsert(pool)
        instance = ComputeProviderInstanceRecord(
            id=str(uuid4()),
            provider="aws",
            offer_id="m7i.xlarge:us-east-1",
            instance_type="m7i.xlarge",
            instance_id="i-0123456789abcdef0",
            status="running",
            source="pooled",
            pool_id=pool.id,
        )
        repository = ComputeProviderInstanceRepository(session)
        repository.upsert(instance)

        machine_id = str(uuid4())
        MachineRepository(session).upsert(
            Machine(id=machine_id, pool=pool.pool, provider="aws"),
            workspace_id=workspace_id,
        )

        bound = repository.bind_machine(pool.id, instance.instance_id or "", machine_id)
        assert bound is not None
        assert bound.machine_id == machine_id
        assert repository.bind_machine(pool.id, instance.instance_id or "", machine_id) == bound
        assert repository.bind_machine(pool.id, instance.instance_id or "", str(uuid4())) is None
        assert repository.bind_machine(pool.id, "i-0ffffffffffffffff", machine_id) is None


def test_reconciliation_preserves_unproved_cleanup_and_reappearing_instances(
    service_context: ServiceContext,
) -> None:
    with service_context.database.session() as session:
        workspace_id = service_context.default_workspace_id(session)
        pool = ComputeUnitRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            name=UnitName("reconcile-history"),
            pool=MachinePool("reconcile-history"),
        )
        ComputeUnitRepository(session).upsert(pool)
        repository = ComputeProviderInstanceRepository(session)
        complete = repository.upsert(
            ComputeProviderInstanceRecord(
                id=str(uuid4()),
                provider="aws",
                offer_id="m7i.xlarge:us-east-1",
                instance_id="i-complete",
                pool_id=pool.id,
                status="deleted",
                source="pooled",
                launch_attempt=7,
                metadata={
                    "missing_since": utc_now().isoformat(),
                    "provider_storage_destroyed_at": utc_now().isoformat(),
                },
            )
        )
        unproved = repository.upsert(
            complete.model_copy(
                update={"id": str(uuid4()), "instance_id": "i-unproved", "metadata": {}}
            )
        )
        active = repository.upsert(
            complete.model_copy(
                update={
                    "id": str(uuid4()),
                    "instance_id": "i-active",
                    "status": "active",
                    "launch_attempt": 1,
                    "metadata": {},
                }
            )
        )
        assert {
            row.id
            for row in repository.list_for_reconciliation(
                pool.id, terminal_statuses=("deleted", "failed"), observed_instance_ids=()
            )
        } == {unproved.id, active.id}
        assert {
            row.id
            for row in repository.list_for_reconciliation(
                pool.id,
                terminal_statuses=("deleted", "failed"),
                observed_instance_ids=("i-complete",),
            )
        } == {complete.id, unproved.id, active.id}
        assert repository.highest_launch_attempt(pool.id) == 7
        for record in (complete, unproved, active):
            session.execute(
                update(ComputeProviderInstanceTable)
                .where(ComputeProviderInstanceTable.id == record.id)
                .values(payload=record.model_dump(mode="json", exclude={"launch_attempt"}))
            )
        assert repository.highest_launch_attempt(pool.id, default=7) == 1
        assert repository.highest_launch_attempt(str(uuid4()), default=7) == 7


def test_pool_sizing_counts_retiring_capacity_until_release_is_terminal(
    service_context: ServiceContext,
) -> None:
    with service_context.database.session() as session:
        workspace_id = service_context.default_workspace_id(session)
        pool = ComputeUnitRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            name=UnitName("sizing"),
            pool=MachinePool("sizing"),
        )
        ComputeUnitRepository(session).upsert(pool)
        repository = ComputeProviderInstanceRepository(session)
        now = utc_now()
        for index, status in enumerate(("active", "deleted", "failed", "terminating")):
            repository.upsert(
                ComputeProviderInstanceRecord(
                    id=str(uuid4()),
                    provider="aws",
                    offer_id="m7i.xlarge:us-east-1",
                    instance_id=f"i-sizing-{index}",
                    pool_id=pool.id,
                    status=status,
                    source="pooled",
                )
            )
            session.execute(
                update(ComputeProviderInstanceTable)
                .where(
                    ComputeProviderInstanceTable.pool_id == pool.id,
                    ComputeProviderInstanceTable.status == status,
                )
                .values(updated_at=now + timedelta(seconds=index))
            )
        summary = repository.sizing_summary_for_pool(
            pool.id, terminal_statuses=("deleted", "failed")
        )
        assert summary.open_count == 2
        assert summary.last_released_at == now + timedelta(seconds=2)
