from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from database.repositories.compute import (
    ComputeJoinCredentialRepository,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from shared.capacity import (
    CapacityOwnerKind,
    capacity_owner_for_provider,
)
from shared.compute_enrollment import ComputeCredentialStatus
from shared.compute_fleet import Machine, MachineLifecycle, ResourceStatus, Worker
from shared.compute_policy import (
    ComputeUnitPhase,
    ComputeUnitRecord,
    UnitName,
)
from shared.container_requests import OciRuntimeName
from shared.errors import (
    ConflictError,
    NotFoundError,
)
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.identity import WorkspaceStatus
from shared.placement import Placement
from shared.routing import PrivateUnitFallback
from shared.timestamps import utc_now

from compute.context import ComputeContext
from compute.machine_lifecycle import write_machine_lifecycle
from compute.pool_provider import PoolProviderService


@dataclass(frozen=True, slots=True)
class ComputeUnitService:
    context: ComputeContext
    providers: PoolProviderService

    def create_unit(
        self,
        name: UnitName,
        *,
        workspace: str = "default",
        placement: Placement | None = None,
        provider: str = "local",
        capacity_owner_id: str | None = None,
        initial_machines: int = 0,
        min_machines: int = 0,
        max_machines: int = 1,
        scaling_enabled: bool = False,
        priority: int = 0,
        min_free_cpu_millicores: int = 0,
        min_free_memory_mib: int = 0,
        min_free_gpu_count: int = 0,
        worker_cpu_millicores: int = 0,
        worker_memory_mib: int = 0,
        worker_gpu_type: str = "",
        worker_gpu_count: int = 0,
        worker_runtimes: tuple[str, ...] = (OciRuntimeName.Runsc.value,),
        worker_preemptible: bool = False,
        idle_drain_timeout_seconds: int = 300,
        scale_up_cooldown_seconds: int = 5,
        scale_down_cooldown_seconds: int = 60,
        registration_timeout_seconds: int = 600,
        fallback: PrivateUnitFallback = PrivateUnitFallback.Internal,
    ) -> ComputeUnitRecord:
        """Create or update a provisioning unit the workspace owns directly.

        Without a placement the unit is platform capacity.
        """
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = ComputeUnitRepository(session)
            existing = repository.get_by_name(workspace_id, name, for_update=True)
            if existing is not None and existing.provider != provider:
                raise ConflictError(f"compute pool provider is immutable: {name}")
            if (
                existing is not None
                and capacity_owner_id is not None
                and existing.capacity_owner_id != capacity_owner_id
            ):
                raise ConflictError(f"compute pool capacity owner is immutable: {name}")
            owner_kind, owner_source = capacity_owner_for_provider(provider)
            owner = capacity_owner_id or str(uuid4())
            saved = repository.upsert(
                ComputeUnitRecord(
                    id=existing.id if existing is not None else owner,
                    capacity_owner_id=(
                        existing.capacity_owner_id if existing is not None else owner
                    ),
                    capacity_owner_kind=(
                        existing.capacity_owner_kind if existing is not None else owner_kind
                    ),
                    capacity_owner_source=(
                        existing.capacity_owner_source if existing is not None else owner_source
                    ),
                    workspace_id=workspace_id,
                    name=name,
                    placement=placement or Placement.platform(),
                    provider=provider,
                    selector=name,
                    status=ComputeUnitPhase.Ready.value,
                    source="workspace",
                    initial_machines=initial_machines,
                    desired_machines=(
                        existing.desired_machines if existing is not None else min_machines
                    ),
                    min_machines=min_machines,
                    max_machines=max_machines,
                    observed_machines=(existing.observed_machines if existing is not None else 0),
                    generation=existing.generation if existing is not None else 1,
                    phase=ComputeUnitPhase.Ready,
                    scaling_enabled=scaling_enabled,
                    priority=priority,
                    min_free_cpu_millicores=min_free_cpu_millicores,
                    min_free_memory_mib=min_free_memory_mib,
                    min_free_gpu_count=min_free_gpu_count,
                    worker_cpu_millicores=worker_cpu_millicores,
                    worker_memory_mib=worker_memory_mib,
                    worker_gpu_type=worker_gpu_type,
                    worker_gpu_count=worker_gpu_count,
                    worker_runtimes=worker_runtimes,
                    worker_preemptible=worker_preemptible,
                    idle_drain_timeout_seconds=idle_drain_timeout_seconds,
                    scale_up_cooldown_seconds=scale_up_cooldown_seconds,
                    scale_down_cooldown_seconds=scale_down_cooldown_seconds,
                    registration_timeout_seconds=registration_timeout_seconds,
                    fallback=fallback,
                )
            )
        self.providers.publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputeUnits,
            change=(
                WorkspaceChangeType.Created if existing is None else WorkspaceChangeType.Updated
            ),
            resource_id=saved.id,
        )
        return saved

    def list_units(self, *, workspace: str = "default") -> list[ComputeUnitRecord]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            records = ComputeUnitRepository(session).list_for_workspace(
                workspace_id, include_retired_platform=False
            )
        return sorted(records, key=lambda item: item.name)

    def list_units_across_workspaces(
        self, *, capacity_owner_kind: CapacityOwnerKind | None = None
    ) -> list[ComputeUnitRecord]:
        """Return current provisioning units with their workspace and capacity owner."""
        with self.context.database.session() as session:
            records = ComputeUnitRepository(session).list_across_workspaces(
                capacity_owner_kind=capacity_owner_kind
            )
        return sorted(records, key=lambda item: (item.workspace_id, item.name))

    def list_pools_for_workspace_deletion(self, workspace_id: str) -> list[ComputeUnitRecord]:
        with self.context.database.session() as session:
            workspace = WorkspaceRepository(session).lock_for_deletion(workspace_id)
            if workspace.status is not WorkspaceStatus.Deleting:
                raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
            records = ComputeUnitRepository(session).list_for_workspace(workspace_id)
        records.sort(key=lambda item: item.name)
        return records

    def platform_units(self) -> list[ComputeUnitRecord]:
        with self.context.database.session() as session:
            namespace = WorkspaceRepository(session).platform()
            if namespace is None:
                raise NotFoundError("platform namespace is not initialized")
            return ComputeUnitRepository(session).list_for_workspace(namespace.id)

    def empty_joined_units(self) -> list[ComputeUnitRecord]:
        with self.context.database.session() as session:
            return ComputeUnitRepository(session).empty_joined_units()

    def delete_empty_joined_unit(self, unit: ComputeUnitRecord) -> bool:
        leases = self.providers.required_capacity_owner_mutations()
        with (
            leases.mutation_lock(unit.capacity_owner_id),
            leases.dispatch_lock(unit.capacity_owner_id),
            self.context.database.session() as session,
        ):
            workspace = WorkspaceRepository(session).get(unit.workspace_id)
            if workspace is None:
                return False
            if workspace.status is not WorkspaceStatus.Active:
                return False
            WorkspaceRepository(session).lock_active_owner(unit.workspace_id)
            units = ComputeUnitRepository(session)
            current = units.get_by_capacity_owner_id(unit.capacity_owner_id, for_update=True)
            if (
                current is None
                or current.workspace_id != unit.workspace_id
                or current.provider != "agent"
                or current.platform_fleet
                or current.capacity_owner_kind is not CapacityOwnerKind.WorkspaceAgent
                or leases.has_open_reservations(current.capacity_owner_id)
            ):
                return False
            if ComputeMachineEnrollmentRepository(session).list_for_unit(
                current.workspace_id, current.capacity_owner_id
            ):
                return False
            credentials = ComputeJoinCredentialRepository(session)
            issued = credentials.list_for_unit(
                current.workspace_id, current.capacity_owner_id, for_update=True
            )
            now = utc_now()
            if not issued or any(
                credential.status is ComputeCredentialStatus.Active
                and credential.expires_at > now
                and credential.use_count < credential.max_uses
                for credential in issued
            ):
                return False
            containers = ContainerRepository(session)
            machines = MachineRepository(session).list_for_capacity_owner(
                current.workspace_id, current.capacity_owner_id
            )
            if any(
                machine.lifecycle is not MachineLifecycle.Deleted
                or containers.count_live_for_machine(machine.id)
                for machine in machines
            ):
                return False
            credentials.delete_for_unit(current.workspace_id, current.capacity_owner_id)
            units.delete(current.id, workspace_id=current.workspace_id)
        self.providers.publish_change(
            workspace_id=unit.workspace_id,
            topic=WorkspaceChangeTopic.ComputeUnits,
            change=WorkspaceChangeType.Deleted,
            resource_id=unit.capacity_owner_id,
        )
        return True

    def list_machines(self, *, workspace: str = "default") -> list[Machine]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            records = [
                machine
                for machine in MachineRepository(session).list(workspace_id=workspace_id)
                if machine.lifecycle is not MachineLifecycle.Deleted
            ]
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def delete_machine(self, machine_id: str, *, workspace: str = "default") -> None:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            machines = MachineRepository(session)
            machine = machines.get(machine_id, workspace_id=workspace_id)
            if machine is None:
                msg = f"machine not found in workspace: {machine_id}"
                raise KeyError(msg)
            if machine.lifecycle is MachineLifecycle.Deleted:
                return
            write_machine_lifecycle(
                session,
                machine,
                MachineLifecycle.Deleted,
                workspace_changes=self.providers.workspace_changes,
                workspace_id=workspace_id,
                message="Removed",
            )

    def list_workers(self) -> list[Worker]:
        with self.context.database.session() as session:
            records = [
                worker
                for worker in WorkerRepository(session).list_across_workspaces()
                if worker.status is not ResourceStatus.Deleted
            ]
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def delete_worker(self, worker_id: str) -> None:
        with self.context.database.session() as session:
            repository = WorkerRepository(session)
            workspace_id = repository.workspace_id(worker_id)
            repository.delete_across_workspaces(worker_id)
        if workspace_id is not None:
            self.providers.publish_change(
                workspace_id=workspace_id,
                topic=WorkspaceChangeTopic.ComputeWorkers,
                change=WorkspaceChangeType.Deleted,
                resource_id=worker_id,
            )

    def clear_capacity_degradation(
        self,
        workspace: str,
        capacity_owner_id: str,
    ) -> ComputeUnitRecord:
        """Explicitly reopen failed capacity and advance its launch-attempt baseline."""
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = ComputeUnitRepository(session)
            unit = repository.get_by_capacity_owner_id(capacity_owner_id, for_update=True)
            # The owner id is globally unique, so the lookup crosses workspaces;
            # a unit belonging to another one reports as missing rather than as
            # forbidden, which would confirm the id exists.
            if unit is None or unit.workspace_id != workspace_id:
                raise NotFoundError(f"compute unit {capacity_owner_id!r} not found")
            highest = ComputeProviderInstanceRepository(session).highest_launch_attempt(
                unit.id, default=unit.provider_state.launch_attempt_baseline
            )
            cleared = repository.apply_provider_state(
                unit.id,
                generation=unit.generation,
                observed_machines=unit.observed_machines,
                phase=ComputeUnitPhase.Ready,
                provider_state=unit.provider_state.model_copy(
                    update={
                        "degraded_reason": None,
                        "degraded_at": None,
                        "launch_attempt_baseline": highest,
                    }
                ),
            )
        if cleared is None:
            raise ConflictError(
                f"compute unit {capacity_owner_id!r} changed while clearing degradation"
            )
        self.providers.publish_change(
            workspace_id=cleared.workspace_id,
            topic=WorkspaceChangeTopic.ComputeUnits,
            change=WorkspaceChangeType.Updated,
            resource_id=cleared.id,
        )
        return cleared
