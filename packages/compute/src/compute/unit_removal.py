from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from database.repositories.compute import (
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import MachineRepository
from shared.capacity import CapacityOwnerKind
from shared.compute_fleet import MachineLifecycle
from shared.compute_policy import (
    ComputeUnitPhase,
    ComputeUnitRecord,
)
from shared.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.identity import WorkspaceStatus
from shared.timestamps import utc_now

from compute.aws_connections import AwsAccountPoolDrain
from compute.context import ComputeContext
from compute.fleet_reserves import with_retained_machines
from compute.machine_lifecycle import write_machine_lifecycle
from compute.pool_provider import PoolProviderService
from compute.provider_machines import _reservation_open

LOGGER = logging.getLogger(__name__)


def _owns_provider_pool_capacity(pool: ComputeUnitRecord) -> bool:
    """Whether a provider owns the lifecycle of this capacity unit."""

    return pool.capacity_owner_kind is CapacityOwnerKind.PooledProvider and bool(pool.provider_ref)


@dataclass(frozen=True, slots=True)
class UnitRemovalService:
    context: ComputeContext
    providers: PoolProviderService

    def delete_platform_unit(self, capacity_owner_id: str) -> None:
        leases = self.providers.required_capacity_owner_mutations()
        with leases.mutation_lock(capacity_owner_id), leases.dispatch_lock(capacity_owner_id):
            with self.context.database.session() as session:
                namespace = WorkspaceRepository(session).platform()
                unit = ComputeUnitRepository(session).get_by_capacity_owner_id(capacity_owner_id)
                if unit is None:
                    return
                if (
                    namespace is None
                    or unit.workspace_id != namespace.id
                    or not unit.platform_fleet
                ):
                    raise InvalidInputError("capacity does not belong to this deployment")
                if ComputeProviderInstanceRepository(session).count_busy_machines(unit.id):
                    raise ConflictError("platform capacity still holds active work")
            if leases.has_open_reservations(capacity_owner_id):
                raise ConflictError("platform capacity still holds scheduling reservations")
            self.delete_unit(capacity_owner_id, workspace=namespace.id)

    def delete_unit(self, capacity_owner_id: str, *, workspace: str = "default") -> None:
        termination_errors: list[str] = []
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(capacity_owner_id)
            if unit is None:
                return
            if unit.workspace_id != workspace_id:
                raise NotFoundError(f"compute unit not found: {capacity_owner_id}")
        clients = self.providers.provider_client_snapshot(workspace_id)
        with self.context.database.session() as session:
            compute_pool = ComputeUnitRepository(session).get_by_capacity_owner_id(
                capacity_owner_id
            )
            if compute_pool is not None:
                provider_instances = ComputeProviderInstanceRepository(session)
                for record in provider_instances.list_for_pool(compute_pool.id):
                    if not _reservation_open(record.status):
                        continue
                    self.providers.machines._terminate_provider_record(
                        session,
                        record,
                        clients=clients,
                        reason="pool_deleted",
                        message="managed compute pool deleted",
                    )
        if compute_pool is not None and _owns_provider_pool_capacity(compute_pool):
            release_error = self.release_provider_pool_capacity(compute_pool)
            if release_error:
                termination_errors.append(release_error)
        with self.context.database.session() as session:
            compute_pool_repository = ComputeUnitRepository(session)
            compute_pool = (
                compute_pool_repository.get(compute_pool.id) if compute_pool is not None else None
            )
            if compute_pool is not None:
                provider_instances = ComputeProviderInstanceRepository(session)
                for record in provider_instances.list_for_pool(compute_pool.id):
                    if not _reservation_open(record.status):
                        continue
                    detail = record.last_error or "provider unavailable"
                    termination_errors.append(f"{record.provider}/{record.id}: {detail}")
            if not termination_errors:
                machine_repository = MachineRepository(session)
                owner = compute_pool.capacity_owner_id if compute_pool is not None else ""
                for machine in machine_repository.list_for_capacity_owner(
                    workspace_id=workspace_id,
                    capacity_owner_id=owner,
                    exclude_deleted=True,
                ):
                    write_machine_lifecycle(
                        session,
                        machine,
                        MachineLifecycle.Deleted,
                        workspace_changes=self.providers.workspace_changes,
                        workspace_id=workspace_id,
                        message="Unit deleted",
                    )
                unit = ComputeUnitRepository(session).get_by_capacity_owner_id(capacity_owner_id)
                # Pooled units retain terminal ownership and recovery history.
                if unit is not None and not _owns_provider_pool_capacity(unit):
                    ComputeUnitRepository(session).delete(
                        unit.id,
                        workspace_id=workspace_id,
                    )
        if termination_errors:
            details = "; ".join(termination_errors)
            raise UpstreamUnavailableError(
                f"managed compute pool capacity could not be terminated: {details}"
            )
        self.providers.publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputeUnits,
            change=WorkspaceChangeType.Deleted,
            resource_id=capacity_owner_id,
        )

    def release_provider_pool_capacity(self, pool: ComputeUnitRecord) -> str:
        """Persist deletion intent; return an error until provider absence is proven."""

        with self.context.database.session() as session:
            repository = ComputeUnitRepository(session)
            current = repository.get(pool.id, for_update=True)
            if current is None:
                return ""
            if (
                current.phase not in {ComputeUnitPhase.Deleting, ComputeUnitPhase.Deleted}
                or current.desired_machines
                or current.min_machines
                or current.stopped_machines
            ):
                current = repository.upsert(
                    current.model_copy(
                        update={
                            "desired_machines": 0,
                            "stopped_machines": 0,
                            "retiring_stopped_machines": current.retiring_stopped_machines
                            + current.stopped_machines,
                            "min_machines": 0,
                            "replacement_machine_id": "",
                            "replacement_template_version": "",
                            "generation": current.generation + 1,
                            "phase": ComputeUnitPhase.Deleting,
                            "status": ComputeUnitPhase.Deleting.value,
                        }
                    )
                )
        try:
            durable, provider, offer = self.providers.internal_unit_provider(
                current.workspace_id,
                current.capacity_owner_id,
            )
            pooled = provider.pooled
            if pooled is None:
                raise RuntimeError(f"compute pool {current.name!r} provider is not pooled")
            released = self.providers.machines._apply_pooled_snapshot(
                durable,
                offer,
                pooled.delete_unit(self.providers.provider_unit_request(durable, offer)),
                provider=pooled,
            )
        except Exception as exc:
            LOGGER.exception(
                "provider pool capacity release failed for pool %s (%s)",
                current.name,
                current.id,
            )
            return f"{current.provider_ref}/{current.name}: {exc}"
        if released.phase is not ComputeUnitPhase.Deleted:
            return (
                f"{current.provider_ref}/{current.name}: provider capacity release is "
                f"in progress ({released.phase.value})"
            )
        return ""

    def delete_unit_for_workspace_deletion(
        self, capacity_owner_id: str, *, workspace_id: str
    ) -> None:
        """Delete one existing pool without reopening a Deleting workspace."""
        termination_errors: list[str] = []
        clients = self.providers.provider_client_snapshot_for_workspace_deletion(workspace_id)
        with self.context.database.session() as session:
            workspace = WorkspaceRepository(session).lock_for_deletion(workspace_id)
            if workspace.status is not WorkspaceStatus.Deleting:
                raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
            compute_pool_repository = ComputeUnitRepository(session)
            compute_pool = compute_pool_repository.get_by_capacity_owner_id(capacity_owner_id)
            if compute_pool is not None and compute_pool.workspace_id != workspace_id:
                raise NotFoundError(f"compute unit not found: {capacity_owner_id}")
            if compute_pool is not None:
                provider_instances = ComputeProviderInstanceRepository(session)
                for record in provider_instances.list_for_pool(compute_pool.id):
                    if not _reservation_open(record.status):
                        continue
                    self.providers.machines._terminate_provider_record(
                        session,
                        record,
                        clients=clients,
                        reason="workspace_deleted",
                        message="workspace deletion terminated managed compute pool",
                        deleting_workspace_id=workspace_id,
                    )
                for record in provider_instances.list_for_pool(compute_pool.id):
                    if _reservation_open(record.status):
                        detail = record.last_error or "provider unavailable"
                        termination_errors.append(f"{record.provider}/{record.id}: {detail}")
                if (
                    _owns_provider_pool_capacity(compute_pool)
                    and compute_pool.phase is not ComputeUnitPhase.Deleted
                ):
                    # Workspace deletion only begins once the provider account is
                    # disconnected, so nothing here can still reach the provider to
                    # release a pool. The deleted phase the drain recorded is the
                    # only proof the provider holds nothing, and dropping the row
                    # without it orphans an Auto Scaling group no record can name.
                    termination_errors.append(
                        f"{compute_pool.provider_ref}/{compute_pool.name}: provider pool "
                        f"capacity is still held ({compute_pool.phase.value})"
                    )
            if termination_errors:
                details = "; ".join(termination_errors)
                raise UpstreamUnavailableError(
                    f"managed compute pool capacity could not be terminated: {details}"
                )
            if compute_pool is not None:
                compute_pool_repository.delete_for_workspace_deletion(
                    compute_pool.id,
                    workspace_id=workspace_id,
                )
            machine_repository = MachineRepository(session)
            owner = compute_pool.capacity_owner_id if compute_pool is not None else ""
            for machine in machine_repository.list_for_capacity_owner(
                workspace_id=workspace_id,
                capacity_owner_id=owner,
                exclude_deleted=True,
            ):
                machine_repository.mark_deleted_for_workspace_deletion(
                    machine.id,
                    workspace_id=workspace_id,
                )
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(capacity_owner_id)
            if unit is not None:
                ComputeUnitRepository(session).delete_for_workspace_deletion(
                    unit.id,
                    workspace_id=workspace_id,
                )

    def request_connection_drain(
        self,
        connection_id: str,
        *,
        workspace_ids: Sequence[str],
    ) -> AwsAccountPoolDrain:
        """Drain all units backed by this connection across its owner's workspaces."""
        owned = set(workspace_ids)
        mutations = self.providers.required_capacity_owner_mutations()
        with self.context.database.session() as session:
            units = ComputeUnitRepository(session)
            # Include purchases admitted before the connection stopped accepting work.
            units.lock_platform_capacity()
            for workspace_id in sorted(owned):
                units.lock_capacity_workspace(workspace_id)
            pools = units.list_for_provider_connection(connection_id)
        if any(unit.workspace_id not in owned for unit in pools):
            raise UpstreamUnavailableError("AWS capacity ownership is inconsistent")

        for unit in pools:
            try:
                with (
                    mutations.mutation_lock(unit.capacity_owner_id),
                    mutations.dispatch_lock(unit.capacity_owner_id),
                ):
                    with self.context.database.session() as session:
                        units = ComputeUnitRepository(session)
                        if unit.platform_fleet:
                            units.lock_platform_capacity()
                        else:
                            units.lock_capacity_workspace(unit.workspace_id)
                        current = units.get(unit.id, for_update=True)
                        if current is None:
                            raise UpstreamUnavailableError("AWS capacity disappeared during drain")
                        if current.phase not in {
                            ComputeUnitPhase.Deleting,
                            ComputeUnitPhase.Deleted,
                        }:
                            current = units.upsert(
                                with_retained_machines(current, 0).model_copy(
                                    update={
                                        "desired_machines": 0,
                                        "stopped_machines": 0,
                                        "retiring_stopped_machines": (
                                            current.retiring_stopped_machines
                                            + current.stopped_machines
                                        ),
                                        "generation": current.generation + 1,
                                        "phase": ComputeUnitPhase.Deleting,
                                        "status": ComputeUnitPhase.Deleting.value,
                                        "replacement_machine_id": "",
                                        "replacement_template_version": "",
                                    }
                                )
                            )
                    if current.phase is ComputeUnitPhase.Deleted:
                        self.providers.retire_proven_provider_pool_machines(current, now=utc_now())
                        continue
                    provider, offer = self.providers.resolved_internal_unit_provider(current)
                    if provider.pooled is None:
                        raise RuntimeError("AWS capacity provider is not pooled")
                    snapshot = provider.pooled.delete_unit(
                        self.providers.provider_unit_request(current, offer)
                    )
                    self.providers.machines._apply_pooled_snapshot(
                        current,
                        offer,
                        snapshot,
                        provider=provider.pooled,
                    )
            except ConflictError:
                raise
            except Exception as exc:
                raise UpstreamUnavailableError(
                    f"AWS capacity {unit.name!r} could not be drained"
                ) from exc

        with self.context.database.session() as session:
            remaining = sum(
                unit.phase is not ComputeUnitPhase.Deleted
                for unit in ComputeUnitRepository(session).list_for_provider_connection(
                    connection_id
                )
            )
        return AwsAccountPoolDrain(total_pools=len(pools), remaining_pools=remaining)
