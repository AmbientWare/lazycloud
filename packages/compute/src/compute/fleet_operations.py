"""Operator capacity changes through the fleet's durable lifecycle owners."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.repositories.capacity_maintenance import CapacityMaintenanceRepository
from database.repositories.capacity_recovery import CapacityRecoveryRepository
from database.repositories.compute import (
    ComputeMachineEnrollmentRecord,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.orchestration import ContainerRepository, MachineRepository
from database.repositories.worker_releases import WorkerReleaseRepository
from database.types import DatabaseSession
from observability.workspace_changes import WorkspaceChangePublisher
from shared.compute_enrollment import AgentCapacityState, ComputeMachineEnrollmentStatus
from shared.compute_fleet import Machine, MachineLifecycle
from shared.compute_policy import ENDED_UNIT_PHASES, ComputeUnitRecord, ComputeUnitVisibility
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.timestamps import utc_now

from compute.machine_lifecycle import write_machine_lifecycle
from compute.offers import ReservationStatus
from compute.pool_provider import PoolProviderService
from compute.unit_scaling import UnitScalingService


def request_machine_stop(
    session: DatabaseSession,
    *,
    unit: ComputeUnitRecord,
    record: ComputeProviderInstanceRecord,
    machine: Machine,
    enrollment: ComputeMachineEnrollmentRecord,
    desired: int,
    stopped: int,
    workspace_changes: WorkspaceChangePublisher | None,
    now: datetime,
) -> ComputeUnitRecord:
    if (
        record.pool_id != unit.id
        or record.machine_id != machine.id
        or enrollment.machine_id != machine.id
        or enrollment.capacity_owner_id != unit.capacity_owner_id
        or enrollment.workspace_id != unit.workspace_id
    ):
        raise ConflictError("machine stop ownership changed")
    if (
        enrollment.status is not ComputeMachineEnrollmentStatus.Active
        or enrollment.capacity_notice_at is not None
        or enrollment.capacity_state
        not in {AgentCapacityState.Available, AgentCapacityState.Draining}
        or machine.lifecycle not in {MachineLifecycle.Ready, MachineLifecycle.Draining}
        or record.status != ReservationStatus.Active.value
        or record.first_served_at is None
        or record.missing_since is not None
        or record.terminating_reason
    ):
        raise ConflictError("machine must have active serving intake before a planned stop")
    if desired < 0 or desired > unit.desired_machines or stopped < max(unit.stopped_machines, 1):
        raise ConflictError("machine stop requires a reserved stopped slot")
    if ContainerRepository(session).count_live_for_machine(machine.id):
        raise ConflictError("machine must finish its workloads before stopping")
    ComputeMachineEnrollmentRepository(session).save(
        enrollment.model_copy(
            update={
                "capacity_state": AgentCapacityState.Draining,
                "capacity_reason": "machine returning to stopped reserve",
                "capacity_observed_at": now,
            }
        )
    )
    updated = ComputeUnitRepository(session).upsert(
        unit.model_copy(
            update={
                "desired_machines": desired,
                "min_machines": min(unit.min_machines, desired),
                "stopped_machines": stopped,
                "max_machines": max(unit.max_machines, desired + stopped),
                "generation": unit.generation + 1,
            }
        )
    )
    ComputeProviderInstanceRepository(session).upsert(
        record.model_copy(update={"status": ReservationStatus.Stopping.value})
    )
    write_machine_lifecycle(
        session,
        machine,
        MachineLifecycle.Stopping,
        workspace_changes=workspace_changes,
        workspace_id=unit.workspace_id,
        message="Waiting for tenant storage cleanup before stopping",
        now=now,
    )
    return updated


def _platform_unit(session: DatabaseSession, unit_id: str) -> ComputeUnitRecord:
    unit = ComputeUnitRepository(session).get(unit_id, for_update=True)
    if unit is None:
        raise NotFoundError(f"compute unit not found: {unit_id}")
    if not unit.platform_fleet or unit.visibility is not ComputeUnitVisibility.Internal:
        raise InvalidInputError("operator fleet changes require a platform-managed unit")
    if unit.phase in ENDED_UNIT_PHASES:
        raise ConflictError("retired platform capacity cannot be stopped or resumed")
    if (
        unit.replacement_machine_id
        or CapacityMaintenanceRepository(session).active_for_pools([unit.id])
        or CapacityRecoveryRepository(session).unit_has_active_recovery(unit.id)
    ):
        raise ConflictError("platform unit has active maintenance or interruption recovery")
    return unit


@dataclass(slots=True)
class FleetOperations:
    providers: PoolProviderService
    scaling: UnitScalingService

    def stop_machine(self, *, unit_id: str, machine_id: str) -> ComputeUnitRecord:
        mutations = self.providers.required_capacity_owner_mutations()
        hooks = self.providers.scheduler_hooks
        if hooks is None:
            raise ConflictError("operator stops require scheduler worker state")
        with mutations.mutation_lock(unit_id), mutations.dispatch_lock(unit_id):
            with self.providers.context.database.session() as session:
                ComputeUnitRepository(session).lock_platform_capacity()
                unit = _platform_unit(session, unit_id)
                provider, offer = self.providers.resolved_internal_unit_provider(unit)
                if provider.pooled is None or not any(
                    candidate.id == offer.id and candidate.capability_key == offer.capability_key
                    for candidate in provider.pooled.list_reserve_offers(
                        root_volume_gib=unit.root_volume_gib
                    )
                ):
                    raise ConflictError("platform unit does not support retained machines")
                record = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
                machine = MachineRepository(session).get(machine_id, workspace_id=unit.workspace_id)
                enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
                    unit.workspace_id, machine_id, for_update=True
                )
                if (
                    record is None
                    or machine is None
                    or enrollment is None
                    or record.pool_id != unit.id
                ):
                    raise NotFoundError("machine is not an enrolled member of this platform unit")
                if record.status in {
                    ReservationStatus.Stopping.value,
                    ReservationStatus.Stopped.value,
                }:
                    if unit.desired_machines and (
                        enrollment.capacity_state is not AgentCapacityState.Draining
                        or enrollment.capacity_notice_at is not None
                    ):
                        raise ConflictError(
                            "external stop has no matching fleet intent; reconcile the unit first"
                        )
                    updated = unit
                else:
                    if WorkerReleaseRepository(session).machine_has_update(machine_id):
                        raise ConflictError("machine has an active runtime update")
                    updated = request_machine_stop(
                        session,
                        unit=unit,
                        record=record,
                        machine=machine,
                        enrollment=enrollment,
                        desired=max(unit.desired_machines - 1, 0),
                        stopped=unit.stopped_machines + 1,
                        workspace_changes=self.providers.workspace_changes,
                        now=utc_now(),
                    )
            hooks.disable_machine(machine_id, "operator requested machine stop")
            hooks.register_internal_unit(updated, offer)
            return updated

    def resume_unit(self, *, unit_id: str, desired: int) -> ComputeUnitRecord:
        """Restore a running target from observed reserves through normal reconciliation.

        Capacity recovery can replace a machine lost after the inventory check.
        """
        if desired <= 0:
            raise InvalidInputError("resume target must be positive")
        mutations = self.providers.required_capacity_owner_mutations()
        with mutations.mutation_lock(unit_id), mutations.dispatch_lock(unit_id):
            with self.providers.context.database.session() as session:
                ComputeUnitRepository(session).lock_platform_capacity()
                unit = _platform_unit(session, unit_id)

            unit, _snapshot = self.scaling.describe_internal_unit(
                unit.workspace_id, unit.capacity_owner_id
            )

            def require_reserves(current: ComputeUnitRecord) -> None:
                if not current.platform_fleet or current.id != unit_id:
                    raise ConflictError("platform unit ownership changed")
                if desired < current.desired_machines:
                    raise InvalidInputError("resume target cannot reduce running capacity")
                needed = desired - current.desired_machines
                if needed == 0:
                    return
                with self.providers.context.database.session() as session:
                    records = ComputeProviderInstanceRepository(session).list_for_pool(
                        unit_id, statuses=(ReservationStatus.Stopped.value,)
                    )
                available = sum(record.missing_since is None for record in records)
                if needed > min(available, current.stopped_machines):
                    raise ConflictError("resume target exceeds existing stopped capacity")

            require_reserves(unit)
            if desired == unit.desired_machines:
                return unit
            return self.scaling.scale_internal_unit(
                unit.workspace_id,
                unit.capacity_owner_id,
                desired,
                before_mutation=require_reserves,
            )
