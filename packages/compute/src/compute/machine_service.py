from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.repositories.compute import (
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.types import DatabaseSession
from shared.capacity import CapacityOwnerKind
from shared.compute_enrollment import MachineBootstrapFailureReason
from shared.compute_fleet import Machine, MachineLifecycle
from shared.compute_policy import ComputeUnitRecord
from shared.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from shared.http.worker_network import WorkerEgressPolicy
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner

from compute.context import ComputeContext
from compute.machine_lifecycle import (
    RETAINED_MACHINE_LIFECYCLES,
    write_machine_lifecycle,
)
from compute.offers import ReservationStatus
from compute.pool_provider import PoolProviderService
from compute.provider_machines import _utc


@dataclass(frozen=True, slots=True)
class ComputeMachineService:
    context: ComputeContext
    providers: PoolProviderService

    def record_provider_node_lifecycle(
        self,
        *,
        pool_id: str,
        provider_instance_id: str,
        lifecycle: MachineLifecycle,
        failure_reason: MachineBootstrapFailureReason | None,
        failure_detail: str = "",
        now: datetime | None = None,
    ) -> Machine:
        with self.context.database.session() as session:
            return self.record_provider_node_lifecycle_in_transaction(
                session,
                pool_id=pool_id,
                provider_instance_id=provider_instance_id,
                lifecycle=lifecycle,
                failure_reason=failure_reason,
                failure_detail=failure_detail,
                now=now,
            )

    def record_provider_node_lifecycle_in_transaction(
        self,
        session: DatabaseSession,
        *,
        pool_id: str,
        provider_instance_id: str,
        lifecycle: MachineLifecycle,
        failure_reason: MachineBootstrapFailureReason | None,
        failure_detail: str = "",
        now: datetime | None = None,
    ) -> Machine:
        """Move a provider node's machine along its lifecycle from what the node reports."""
        if lifecycle is MachineLifecycle.Failed and failure_reason is None:
            raise InvalidInputError("failed provider bootstrap requires a failure reason")
        if lifecycle is not MachineLifecycle.Failed and failure_reason is not None:
            raise InvalidInputError("provider bootstrap failure reason requires failed phase")
        current_time = _utc(now)
        repository = ComputeProviderInstanceRepository(session)
        record = repository.get_for_pool_instance(
            pool_id,
            provider_instance_id,
            for_update=True,
        )
        if record is None:
            raise NotFoundError("provider node is no longer active")
        pool = ComputeUnitRepository(session).get(pool_id)
        if pool is None:
            raise NotFoundError("provider node unit is gone")
        record, machine = self.providers.machines.machine_for_record(
            session, pool=pool, record=record, now=current_time
        )
        if (
            lifecycle is MachineLifecycle.Joining
            and machine.lifecycle in RETAINED_MACHINE_LIFECYCLES
            and record.status != ReservationStatus.Active.value
        ):
            # A reserve joins again when its stream authorizes the resume. Its
            # own report would admit the worker it runs before the stream decides.
            return machine
        if lifecycle is MachineLifecycle.Joining and machine.lifecycle is MachineLifecycle.Ready:
            # A restarted agent reports joining beside its first stream, whose
            # heartbeat may already have made the machine ready. The report is
            # for a failed or draining machine; a ready one stays ready.
            return machine
        updated = write_machine_lifecycle(
            session,
            machine,
            lifecycle,
            workspace_changes=self.providers.workspace_changes,
            message=failure_detail,
            failure=failure_reason,
            now=current_time,
        )
        if lifecycle is MachineLifecycle.Joining and record.first_enrolled_at is None:
            # This survives deletion of the machine row and its foreign-key binding.
            repository.upsert(
                record.model_copy(
                    update={"first_enrolled_at": current_time, "updated_at": current_time}
                )
            )
        return updated

    def provider_machine_unit(self, machine_id: str) -> tuple[str, str] | None:
        """The unit and workspace owning a provider machine, if a unit owns it."""
        with self.context.database.session() as session:
            instance = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
            unit = (
                ComputeUnitRepository(session).get(instance.pool_id)
                if instance is not None and instance.pool_id is not None
                else None
            )
        return (unit.id, unit.workspace_id) if unit is not None else None

    def worker_availability_zone(self, *, unit: ComputeUnitRecord, machine_id: str) -> str:
        if unit.capacity_owner_kind is not CapacityOwnerKind.PooledProvider:
            return ""
        with self.context.database.session() as session:
            instance = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
        if instance is None or instance.pool_id != unit.id:
            raise ConflictError("worker has no provider instance in its capacity unit")
        return instance.availability_zone

    def worker_egress_policy(
        self, *, workspace_id: str, capacity_owner_id: str, machine_id: str
    ) -> WorkerEgressPolicy:
        with self.context.database.session() as session:
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(capacity_owner_id)
        if unit is None or unit.workspace_id != workspace_id:
            raise ConflictError("worker network policy has no workspace-owned capacity unit")
        if not unit.platform_fleet:
            return WorkerEgressPolicy(
                billing_owner=(
                    UsageBillingOwner.ConnectedCloud
                    if unit.provider_connection_id
                    else UsageBillingOwner.SelfHosted
                ),
                verified_at=utc_now(),
            )
        with self.context.database.session() as session:
            bindings = ComputeProviderInstanceRepository(session).machine_bindings_for_pool(unit.id)
        instances = [instance for instance, machine in bindings.items() if machine == machine_id]
        if len(instances) != 1:
            raise ConflictError("worker has no unique provider instance in its capacity unit")
        provider, _ = self.providers.resolved_internal_unit_provider(unit)
        if provider.pooled is None:
            raise UpstreamUnavailableError("worker provider cannot verify its network routes")
        try:
            destinations = provider.pooled.unbilled_network_destinations(unit, instances[0])
        except Exception as exc:
            raise UpstreamUnavailableError("worker provider route evidence is unavailable") from exc
        return WorkerEgressPolicy(
            billing_owner=UsageBillingOwner.PlatformFleet,
            routes=destinations,
            verified_at=utc_now(),
        )

    def internal_unit_machine_by_instance(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> dict[str, str]:
        """Map observed provider instances to their durable machine identities."""
        unit = self.providers.get_internal_unit(workspace_id, capacity_owner_id)
        with self.context.database.session() as session:
            return ComputeProviderInstanceRepository(session).machine_bindings_for_pool(unit.id)
