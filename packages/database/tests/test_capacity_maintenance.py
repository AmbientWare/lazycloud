from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from database.repositories.capacity_maintenance import CapacityMaintenanceRepository
from database.repositories.compute import ComputeUnitRepository
from database.repositories.orchestration import MachineRepository
from database.tables.capacity_maintenance import CapacityMaintenanceTable
from database.tables.compute import ComputeUnitTable
from identity.platform import PlatformNamespaceService
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.capacity_maintenance import (
    CapacityMaintenanceKind,
    CapacityMaintenancePhase,
    CapacityMaintenanceRecord,
)
from shared.compute_fleet import Machine
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    UnitName,
)
from shared.errors import ConflictError
from shared.placement import Placement
from shared.timestamps import utc_now
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from database import DatabaseClient


def test_maintenance_reserves_replacements_once_and_holds_failed_commitments(
    database: DatabaseClient,
) -> None:
    workspace = PlatformNamespaceService(database).initialize()
    pool_id = str(uuid4())
    source_ids = [str(uuid4()), str(uuid4())]
    replacement_id = str(uuid4())
    now = utc_now()
    with database.session() as session:
        ComputeUnitRepository(session).upsert(
            ComputeUnitRecord(
                id=pool_id,
                workspace_id=workspace.id,
                name=UnitName(pool_id),
                placement=Placement.platform(),
                provider="aws",
                provider_ref="aws:fleet",
                platform_fleet=True,
                capacity_mode=ComputeCapacityMode.Pooled,
                visibility=ComputeUnitVisibility.Internal,
                capacity_owner_id=pool_id,
                capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                capacity_owner_source=CapacityOwnerSource.Provider,
                region="us-east-1",
                offer_id=pool_id,
                capability_key=pool_id,
            )
        )
        for machine_id in [*source_ids, replacement_id]:
            MachineRepository(session).upsert(
                Machine(id=machine_id, provider="aws", capacity_owner_id=pool_id),
                workspace_id=workspace.id,
            )
    proposals = [
        CapacityMaintenanceRecord(
            id=str(uuid4()),
            pool_id=pool_id,
            source_machine_id=source_id,
            release_generation=10,
            kind=CapacityMaintenanceKind.Runtime,
            surge_machines=1,
            running_cpu_millicores=8_000,
            hourly_cost_micros=200_000,
            replacement_machine_id=replacement_id,
            created_at=now,
            updated_at=now,
        )
        for source_id in source_ids
    ]

    def reserve(proposal: CapacityMaintenanceRecord) -> CapacityMaintenanceRecord | None:
        try:
            with database.session() as session:
                return CapacityMaintenanceRepository(session).start(proposal)
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, proposals))
    [winner] = [result for result in results if result is not None]
    with database.session() as session:
        repository = CapacityMaintenanceRepository(session)
        assert repository.start(winner).id == winner.id
        assert [record.id for record in repository.active_for_placement(Placement.platform())] == [
            winner.id
        ]
        assert repository.active_for_pools([str(uuid4())]) == []
        assert repository.surge_by_pool([pool_id]) == {pool_id: 1}
        failed = repository.transition(
            winner.id,
            expected_generation=10,
            expected_phase=CapacityMaintenancePhase.Planned,
            phase=CapacityMaintenancePhase.Failed,
            now=now,
            reason="provider outcome unknown",
        )
        commitments = repository.commitments([pool_id])
        assert commitments.operations == 1
        assert commitments.running_cpu_millicores == 8_000
        assert commitments.hourly_cost_micros == 200_000
        assert failed.completed_at is None
        with pytest.raises(IntegrityError, match="requires cleanup"), session.begin_nested():
            session.execute(delete(ComputeUnitTable).where(ComputeUnitTable.id == pool_id))
        unbound = repository.release_deleted_replacement(failed, now=now)
        assert unbound.replacement_machine_id is None
        rebound = repository.transition(
            winner.id,
            expected_generation=10,
            expected_phase=CapacityMaintenancePhase.Failed,
            phase=CapacityMaintenancePhase.Preparing,
            replacement_machine_id=str(uuid4()),
            now=now,
        )
        assert rebound.release_generation == 10
        assert repository.commitments([pool_id]) == commitments
        repository.transition(
            winner.id,
            expected_generation=10,
            expected_phase=CapacityMaintenancePhase.Preparing,
            phase=CapacityMaintenancePhase.Failed,
            now=now,
        )
        repository.retarget(winner.id, expected_generation=10, release_generation=11, now=now)
        with pytest.raises(ConflictError, match="ownership or phase"):
            repository.transition(
                winner.id,
                expected_generation=10,
                expected_phase=CapacityMaintenancePhase.Failed,
                phase=CapacityMaintenancePhase.Complete,
                now=now,
            )
        completed = repository.transition(
            winner.id,
            expected_generation=11,
            expected_phase=CapacityMaintenancePhase.Failed,
            phase=CapacityMaintenancePhase.Complete,
            now=now,
        )
        assert completed.completed_at == now
        assert repository.commitments([pool_id]).operations == 0
        with pytest.raises(ConflictError, match="newer release"):
            repository.start(winner)
        with pytest.raises(IntegrityError, match="ownership cannot"), session.begin_nested():
            session.execute(
                update(CapacityMaintenanceTable)
                .where(CapacityMaintenanceTable.id == winner.id)
                .values(phase="planned", completed_at=None)
            )
    loser = next(proposal for proposal in proposals if proposal.id != winner.id)
    assert reserve(loser) is not None
