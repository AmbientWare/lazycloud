from datetime import timedelta
from uuid import uuid4

from database.context import ServiceContext
from database.repositories.capacity_activations import CapacityActivationRepository
from database.repositories.capacity_sleep_attempts import CapacitySleepAttemptRepository
from database.repositories.compute import (
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.orchestration import MachineRepository
from database.tables.capacity_activations import CapacityActivationTable
from identity.platform import PlatformNamespaceService
from shared.capacity_lifecycle import (
    CapacityActivationKind,
    CapacityRestoreOutcome,
    CapacitySleepMode,
)
from shared.compute_fleet import Machine
from shared.compute_policy import ComputeUnitRecord, UnitName
from shared.placement import Placement
from shared.timestamps import to_utc, utc_now
from sqlalchemy import select


def test_admitted_provision_records_ready_before_provider_inventory_catches_up(
    service_context: ServiceContext,
) -> None:
    requested_at = utc_now()
    ready_at = requested_at + timedelta(seconds=20)
    observed_at = ready_at + timedelta(seconds=10)
    with service_context.database.session() as session:
        workspace_id = service_context.default_workspace_id(session)
        machine = Machine(id=str(uuid4()))
        MachineRepository(session).upsert(machine, workspace_id=workspace_id)
        instance = ComputeProviderInstanceRecord(
            id=str(uuid4()),
            provider="aws",
            offer_id="cpu",
            status="pending",
            source="pooled",
            machine_id=machine.id,
        )
        instances = ComputeProviderInstanceRepository(session)
        instances.upsert(instance)
        repository = CapacityActivationRepository(session)
        repository.observe(
            instance.id,
            requested_at=requested_at,
            kind=CapacityActivationKind.Provision,
            provider_running_at=None,
            observed_at=requested_at,
        )
        repository.record_ready([machine.id], at=ready_at)
        instances.upsert(instance.model_copy(update={"status": "active"}))
        repository.observe(
            instance.id,
            requested_at=requested_at,
            kind=CapacityActivationKind.Provision,
            provider_running_at=requested_at + timedelta(seconds=15),
            observed_at=observed_at,
        )
        repository.record_ready([machine.id], at=observed_at)
        activation = session.scalar(
            select(CapacityActivationTable).where(
                CapacityActivationTable.instance_record_id == instance.id
            )
        )
        assert activation is not None and activation.ready_at is not None
        assert to_utc(activation.ready_at) == ready_at
        assert activation.failed_at is None


def test_activation_cycles_preserve_preparation_failure_and_verified_resume(
    service_context: ServiceContext,
) -> None:
    now = utc_now()
    platform_id = PlatformNamespaceService(service_context.database).namespace_id
    with service_context.database.session() as session:
        workspace_id = service_context.default_workspace_id(session)
        machine = Machine(id=str(uuid4()))
        MachineRepository(session).upsert(machine, workspace_id=workspace_id)
        unit = ComputeUnitRecord(
            id=str(uuid4()),
            workspace_id=platform_id,
            name=UnitName("activation-cycles"),
            placement=Placement.platform(),
            platform_fleet=True,
        )
        ComputeUnitRepository(session).upsert(unit)
        instance = ComputeProviderInstanceRecord(
            id=str(uuid4()),
            provider="aws",
            offer_id="cpu",
            instance_type="cpu",
            status="resuming",
            source="pooled",
            machine_id=machine.id,
            pool_id=unit.id,
        )
        ComputeProviderInstanceRepository(session).upsert(instance)
        repository = CapacityActivationRepository(session)
        repository.observe(
            instance.id,
            requested_at=now,
            kind=CapacityActivationKind.Provision,
            provider_running_at=now + timedelta(seconds=30),
            observed_at=now,
        )
        repository.record_prepared(instance.id, at=now + timedelta(seconds=40))
        sleep = CapacitySleepAttemptRepository(session).begin(
            instance.id,
            attempt_id=str(uuid4()),
            boot_id=str(uuid4()),
            requested_mode=CapacitySleepMode.Hibernate,
            requested_at=now + timedelta(seconds=45),
            observed_at=now + timedelta(seconds=45),
        )
        resumed_at = now + timedelta(hours=1)
        repository.observe(
            instance.id,
            requested_at=resumed_at,
            kind=CapacityActivationKind.Resume,
            provider_running_at=None,
            observed_at=resumed_at,
            sleep_attempt_id=sleep.id,
        )
        authorized_at = resumed_at + timedelta(seconds=7)
        repository.record_restore(
            instance.id,
            sleep_attempt_id=sleep.id,
            at=authorized_at,
            outcome=CapacityRestoreOutcome.MemoryRestored,
        )
        repository.authorize(instance.id, at=authorized_at)
        repository.record_ready([machine.id], at=authorized_at)
        repository.record_failed(instance.id, at=authorized_at + timedelta(seconds=1))
        repository.observe(
            instance.id,
            requested_at=resumed_at,
            kind=CapacityActivationKind.Resume,
            provider_running_at=resumed_at + timedelta(seconds=6),
            observed_at=authorized_at,
        )
        failed_at = resumed_at + timedelta(hours=1)
        repository.observe(
            instance.id,
            requested_at=failed_at,
            kind=CapacityActivationKind.Boot,
            provider_running_at=None,
            observed_at=failed_at,
        )
        repository.record_failed(instance.id, at=failed_at + timedelta(minutes=10))
        repository.record_ready([machine.id], at=failed_at + timedelta(minutes=11))
        rows = session.scalars(
            select(CapacityActivationTable)
            .where(CapacityActivationTable.instance_record_id == instance.id)
            .order_by(CapacityActivationTable.requested_at)
        ).all()
        assert len(rows) == 3
        assert rows[0].prepared_at is not None and rows[0].failed_at is None
        assert rows[1].restore_outcome == CapacityRestoreOutcome.MemoryRestored.value
        assert rows[1].ready_at is not None and to_utc(rows[1].ready_at) == authorized_at
        assert rows[1].failed_at is None
        assert rows[2].failed_at is not None and rows[2].ready_at is None
        summaries = repository.summarize(since=now)
        resume = next(row for row in summaries if row.kind is CapacityActivationKind.Resume)
        assert resume.ready == 1 and resume.failed == 0
        assert resume.p95_ready_seconds == 7
        assert resume.p95_provider_seconds == 6
