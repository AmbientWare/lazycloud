from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe

from database.mappers.autoscaling import autoscaler_state_from_row
from database.mappers.containers import container_from_row, write_container
from database.mappers.fleet import (
    agent_from_row,
    agent_lease_from_row,
    machine_from_row,
    worker_from_row,
    write_agent,
    write_agent_lease,
    write_machine,
    write_worker,
)
from database.records.autoscaling import AutoscalingTargetClaim
from database.repositories.cleanup import CleanupRepository
from database.repositories.identity import WorkspaceRepository
from database.tables.container_rollouts import ContainerRolloutDrainTable
from database.tables.identity import WorkspaceMemberTable
from database.tables.images import ImageBuildAttemptTable
from database.tables.orchestration import (
    AgentLeaseTable,
    AgentTable,
    AutoscalerStateTable,
    AutoscalingTargetTable,
    ContainerTable,
    MachineTable,
    MachineWorkspaceTable,
    WorkerTable,
)
from foundation.ids import try_uuid
from shared.autoscaler_state import (
    AutoscalerStateRecord,
    AutoscalerTargetKind,
)
from shared.compute_fleet import (
    AgentLease,
    AgentRecord,
    Machine,
    MachineLifecycle,
    ResourceStatus,
    Worker,
)
from shared.container_requests import (
    ContainerShutdownTarget,
    StopContainerReason,
)
from shared.containers import LIVE_CONTAINER_STATUSES, ContainerRecord, ContainerStatus
from shared.errors import ConflictError
from shared.identity import WorkspaceRole, WorkspaceStatus
from shared.placement import Placement, PlacementKind
from shared.timestamps import utc_now
from sqlalchemy import (
    Select,
    and_,
    case,
    delete,
    exists,
    false,
    func,
    or_,
    select,
    text,
    tuple_,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnElement


def container_storage_release_pending() -> ColumnElement[bool]:
    return or_(
        ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
        and_(
            ContainerTable.storage_released_at.is_(None),
            or_(
                ContainerTable.worker_id.is_not(None),
                ContainerTable.runtime_worker_id != "",
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class ContainerProgressSnapshot:
    id: str
    workspace_id: str
    stub_id: str | None
    status: ContainerStatus
    worker_id: str | None


@dataclass(slots=True)
class AutoscalingTargetRepository:
    session: Session

    def activate(
        self,
        *,
        stub_id: str,
        workspace_id: str,
        target_kind: AutoscalerTargetKind,
        due_at: datetime | None = None,
    ) -> None:
        current_time = due_at or datetime.now(UTC)
        values: dict[str, str | int | datetime | None] = {
            "stub_id": stub_id,
            "workspace_id": workspace_id,
            "target_kind": target_kind.value,
            "due_at": current_time,
            "generation": 1,
            "claim_token": None,
            "claim_expires_at": None,
            "created_at": current_time,
            "updated_at": current_time,
        }
        insert = postgresql_insert(AutoscalingTargetTable).values(**values)
        earlier_due_at = case(
            (AutoscalingTargetTable.due_at > insert.excluded.due_at, insert.excluded.due_at),
            else_=AutoscalingTargetTable.due_at,
        )
        self.session.execute(
            insert.on_conflict_do_update(
                index_elements=[AutoscalingTargetTable.stub_id],
                set_={
                    "workspace_id": insert.excluded.workspace_id,
                    "target_kind": insert.excluded.target_kind,
                    "due_at": earlier_due_at,
                    "generation": AutoscalingTargetTable.generation + 1,
                    "updated_at": insert.excluded.updated_at,
                },
            )
        )

    def claim_due(
        self,
        *,
        now: datetime | None = None,
        limit: int,
        lease_seconds: float,
    ) -> list[AutoscalingTargetClaim]:
        if limit <= 0:
            return []
        current_time = now or datetime.now(UTC)
        token = token_urlsafe(24)
        statement = (
            select(AutoscalingTargetTable)
            .where(
                AutoscalingTargetTable.due_at <= current_time,
                or_(
                    AutoscalingTargetTable.claim_token.is_(None),
                    AutoscalingTargetTable.claim_expires_at <= current_time,
                ),
            )
            .order_by(AutoscalingTargetTable.due_at.asc(), AutoscalingTargetTable.stub_id.asc())
            .limit(limit)
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        else:
            statement = statement.with_for_update()
        rows = list(self.session.scalars(statement))
        claim_expires_at = current_time + timedelta(seconds=max(lease_seconds, 0.0))
        claims: list[AutoscalingTargetClaim] = []
        for row in rows:
            row.claim_token = token
            row.claim_expires_at = claim_expires_at
            row.updated_at = current_time
            claims.append(
                AutoscalingTargetClaim(
                    stub_id=row.stub_id,
                    workspace_id=row.workspace_id,
                    target_kind=AutoscalerTargetKind(row.target_kind),
                    generation=row.generation,
                    token=token,
                    due_at=row.due_at,
                )
            )
        self.session.flush()
        return claims

    def complete(
        self,
        claim: AutoscalingTargetClaim,
        *,
        next_reconcile_at: datetime | None,
        now: datetime | None = None,
    ) -> bool:
        return (
            self.complete_many(
                ((claim, next_reconcile_at),),
                now=now,
            )
            == 1
        )

    def complete_many(
        self,
        completions: Sequence[tuple[AutoscalingTargetClaim, datetime | None]],
        *,
        now: datetime | None = None,
    ) -> int:
        if not completions:
            return 0
        by_stub_id = {claim.stub_id: (claim, due_at) for claim, due_at in completions}
        statement = select(AutoscalingTargetTable).where(
            AutoscalingTargetTable.stub_id.in_(by_stub_id)
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        rows = list(self.session.scalars(statement))
        completed = 0
        current_time = now or datetime.now(UTC)
        for row in rows:
            claim, next_reconcile_at = by_stub_id[row.stub_id]
            if row.claim_token != claim.token:
                continue
            completed += 1
            if row.generation == claim.generation and next_reconcile_at is None:
                self.session.delete(row)
                continue
            if row.generation == claim.generation and next_reconcile_at is not None:
                row.due_at = next_reconcile_at
            row.claim_token = None
            row.claim_expires_at = None
            row.updated_at = current_time
        self.session.flush()
        return completed


@dataclass(slots=True)
class AutoscalerStateRepository:
    session: Session

    def upsert(self, state: AutoscalerStateRecord) -> AutoscalerStateRecord:
        state = AutoscalerStateRecord.model_validate(dict(state))
        WorkspaceRepository(self.session).lock_active_owner(state.workspace_id)
        insert = postgresql_insert(AutoscalerStateTable).values(
            workspace_id=state.workspace_id,
            source=state.source,
            target_kind=state.target_kind.value,
            target_id=state.target_id,
            deployment_id=state.deployment_id,
            app_id=state.app_id,
            current_count=state.current_count,
            desired_count=state.desired_count,
            signal_name=state.signal_name,
            signal_value=state.signal_value,
            decision=state.decision,
            reason=state.reason,
            active=state.active,
            valid=state.valid,
            lock_acquired=state.lock_acquired,
            owner_lock_key=state.owner_lock_key,
            cooldown_until=state.cooldown_until,
            failed_container_count=state.failed_container_count,
            pending_count=state.pending_count,
            guardrails=state.guardrails,
            last_actions=[action.model_dump(mode="json") for action in state.last_actions],
            updated_at=state.updated_at,
        )
        statement = (
            insert.on_conflict_do_update(
                index_elements=[
                    AutoscalerStateTable.workspace_id,
                    AutoscalerStateTable.target_kind,
                    AutoscalerStateTable.target_id,
                ],
                set_={
                    "source": insert.excluded.source,
                    "deployment_id": insert.excluded.deployment_id,
                    "app_id": insert.excluded.app_id,
                    "current_count": insert.excluded.current_count,
                    "desired_count": insert.excluded.desired_count,
                    "signal_name": insert.excluded.signal_name,
                    "signal_value": insert.excluded.signal_value,
                    "decision": insert.excluded.decision,
                    "reason": insert.excluded.reason,
                    "active": insert.excluded.active,
                    "valid": insert.excluded.valid,
                    "lock_acquired": insert.excluded.lock_acquired,
                    "owner_lock_key": insert.excluded.owner_lock_key,
                    "cooldown_until": insert.excluded.cooldown_until,
                    "failed_container_count": insert.excluded.failed_container_count,
                    "pending_count": insert.excluded.pending_count,
                    "guardrails": insert.excluded.guardrails,
                    "last_actions": insert.excluded.last_actions,
                    "updated_at": insert.excluded.updated_at,
                },
            )
            .returning(AutoscalerStateTable)
            .execution_options(populate_existing=True)
        )
        return autoscaler_state_from_row(self.session.scalars(statement).one())

    def get(
        self, *, workspace_id: str, target_kind: AutoscalerTargetKind, target_id: str
    ) -> AutoscalerStateRecord | None:
        row = self.session.get(AutoscalerStateTable, (workspace_id, target_kind.value, target_id))
        return autoscaler_state_from_row(row) if row is not None else None

    def list(self, *, workspace_id: str, source: str | None = None) -> list[AutoscalerStateRecord]:
        statement = select(AutoscalerStateTable).where(
            AutoscalerStateTable.workspace_id == workspace_id
        )
        if source is not None:
            statement = statement.where(AutoscalerStateTable.source == source)
        return [
            autoscaler_state_from_row(row)
            for row in self.session.scalars(
                statement.order_by(AutoscalerStateTable.target_kind, AutoscalerStateTable.target_id)
            )
        ]

    def list_across_workspaces(self, *, source: str | None = None) -> list[AutoscalerStateRecord]:
        statement = select(AutoscalerStateTable)
        if source is not None:
            statement = statement.where(AutoscalerStateTable.source == source)
        return [
            autoscaler_state_from_row(row)
            for row in self.session.scalars(
                statement.order_by(
                    AutoscalerStateTable.workspace_id,
                    AutoscalerStateTable.target_kind,
                    AutoscalerStateTable.target_id,
                )
            )
        ]


@dataclass(slots=True)
class MachineRepository:
    session: Session

    def upsert(
        self,
        machine: Machine,
        *,
        workspace_id: str | None = None,
        owner_user_id: str | None = None,
        for_workspace_deletion: bool = False,
    ) -> Machine:
        """Write a machine. `owner_user_id` is the account a named machine is unique in.

        Given by the caller that minted the join, because a workspace may have
        several owners and the first row is not the one who joined it.
        `for_workspace_deletion` is the deletion pass writing terminal state on
        rows of a workspace that is no longer active; it fences on the deletion
        lock instead of refusing.
        """
        machine = Machine.model_validate(dict(machine))
        row = self.session.get(MachineTable, machine.id)
        owner_id = workspace_id if workspace_id is not None else row.workspace_id if row else None
        if owner_id is not None:
            workspaces = WorkspaceRepository(self.session)
            if for_workspace_deletion:
                if workspaces.lock_for_deletion(owner_id).status is not WorkspaceStatus.Deleting:
                    raise ConflictError(f"workspace cleanup requires deleting state: {owner_id}")
            else:
                workspaces.lock_active_owner(owner_id)
        if row is None:
            owner = owner_user_id or (
                self.session.scalar(
                    select(WorkspaceMemberTable.user_id).where(
                        WorkspaceMemberTable.workspace_id == owner_id,
                        WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
                    )
                )
                if owner_id is not None
                else None
            )
            row = MachineTable(
                id=machine.id,
                workspace_id=owner_id,
                owner_user_id=owner,
                created_at=machine.created_at,
            )
            self.session.add(row)
        elif row.workspace_id != owner_id:
            raise ConflictError("machine workspace cannot change")
        write_machine(row, machine)
        row.updated_at = utc_now()
        self.session.flush()
        return machine_from_row(row)

    def get_by_owner_name(self, owner_user_id: str, name: str) -> Machine | None:
        """The account's live machine with this name, if one exists.

        Deleted rows keep their name for history; the partial unique index and
        this lookup both leave them out so a name can be joined again.
        """
        row = self.session.scalar(
            select(MachineTable).where(
                MachineTable.owner_user_id == owner_user_id,
                MachineTable.name == name,
                MachineTable.status != ResourceStatus.Deleted.value,
            )
        )
        return machine_from_row(row) if row is not None else None

    def get_serving_by_name(self, workspace_id: str, name: str) -> Machine | None:
        """The live machine with this name that lists the workspace among those it serves.

        Reached through the served-workspace link rather than an owner account, so a
        workspace with several owners resolves the same machine whoever created it.
        """
        row = self.session.scalar(
            select(MachineTable)
            .join(MachineWorkspaceTable, MachineWorkspaceTable.machine_id == MachineTable.id)
            .where(
                MachineWorkspaceTable.workspace_id == workspace_id,
                MachineTable.name == name,
                MachineTable.status != ResourceStatus.Deleted.value,
            )
        )
        return machine_from_row(row) if row is not None else None

    def list_named_for_owner(self, owner_user_id: str) -> list[Machine]:
        statement = select(MachineTable).where(
            MachineTable.owner_user_id == owner_user_id,
            MachineTable.name.is_not(None),
            MachineTable.status != ResourceStatus.Deleted.value,
        )
        return [
            machine_from_row(row)
            for row in self.session.scalars(statement.order_by(MachineTable.name))
        ]

    def list_joined_for_owner(self, owner_user_id: str) -> list[Machine]:
        """The account's machines placed on themselves: the hosts it joined.

        Classified by placement kind, not by provider name: a node a connected
        cloud launched enrolls through the same agent and would otherwise be
        listed as hardware the customer connected.
        """
        statement = select(MachineTable).where(
            MachineTable.owner_user_id == owner_user_id,
            MachineTable.placement.like(f"{PlacementKind.Machine.value}:%"),
            MachineTable.lifecycle != MachineLifecycle.Deleted.value,
        )
        return [
            machine_from_row(row)
            for row in self.session.scalars(statement.order_by(MachineTable.id))
        ]

    def list_for_placement(self, placement: Placement) -> list[Machine]:
        """Every machine still shown under one placement, across its workspaces.

        Deleted rows are history; draining and terminating ones are still the
        placement's until the provider proves them gone.
        """
        statement = select(MachineTable).where(
            MachineTable.placement == placement.key,
            MachineTable.lifecycle != MachineLifecycle.Deleted.value,
        )
        return [
            machine_from_row(row)
            for row in self.session.scalars(
                statement.order_by(MachineTable.created_at.desc(), MachineTable.id)
            )
        ]

    def move_placement_for_capacity_owner(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        placement: Placement,
    ) -> int:
        """Restamp one unit's machines with the placement the unit moved to."""
        result = self.session.execute(
            update(MachineTable)
            .where(
                MachineTable.workspace_id == workspace_id,
                MachineTable.capacity_owner_id == capacity_owner_id,
                MachineTable.placement != placement.key,
            )
            .values(placement=placement.key, updated_at=utc_now())
        )
        return int(result.rowcount) if isinstance(result, CursorResult) else 0

    def get(self, machine_id: str, *, workspace_id: str) -> Machine | None:
        if try_uuid(machine_id) is None:
            return None
        row = self.session.scalar(
            select(MachineTable).where(
                MachineTable.id == machine_id, MachineTable.workspace_id == workspace_id
            )
        )
        return machine_from_row(row) if row is not None else None

    def get_owned(self, machine_id: str, *, owner_user_id: str) -> Machine | None:
        """One of the account's live machines by id, a primary-key read."""
        if try_uuid(machine_id) is None:
            return None
        row = self.session.get(MachineTable, machine_id)
        if (
            row is None
            or row.owner_user_id != owner_user_id
            or row.status == ResourceStatus.Deleted.value
        ):
            return None
        return machine_from_row(row)

    def get_across_workspaces(self, machine_id: str) -> Machine | None:
        if try_uuid(machine_id) is None:
            return None
        row = self.session.get(MachineTable, machine_id)
        return machine_from_row(row) if row is not None else None

    def workspace_id(self, machine_id: str) -> str | None:
        return self.session.scalar(
            select(MachineTable.workspace_id).where(MachineTable.id == machine_id)
        )

    def list(self, *, workspace_id: str, status: str | None = None) -> list[Machine]:
        statement = select(MachineTable).where(MachineTable.workspace_id == workspace_id)
        if status is not None:
            statement = statement.where(MachineTable.status == status)
        return [
            machine_from_row(row)
            for row in self.session.scalars(
                statement.order_by(MachineTable.created_at.desc(), MachineTable.id)
            )
        ]

    def list_across_workspaces(self, *, status: str | None = None) -> list[Machine]:
        statement = select(MachineTable)
        if status is not None:
            statement = statement.where(MachineTable.status == status)
        return [
            machine_from_row(row)
            for row in self.session.scalars(
                statement.order_by(MachineTable.created_at.desc(), MachineTable.id)
            )
        ]

    def mark_deleted_for_workspace_deletion(
        self,
        machine_id: str,
        *,
        workspace_id: str,
    ) -> Machine | None:
        workspace = WorkspaceRepository(self.session).lock_for_deletion(workspace_id)
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
        row = self.session.get(MachineTable, machine_id)
        if row is None:
            return None
        if str(row.workspace_id) != workspace_id:
            raise ConflictError(f"machine is not owned by deleting workspace: {machine_id}")
        now = utc_now()
        row.status = ResourceStatus.Deleted.value
        if row.lifecycle != MachineLifecycle.Deleted.value:
            row.lifecycle = MachineLifecycle.Deleted.value
            row.lifecycle_message = "Removed with its workspace"
            row.lifecycle_at = now
        row.updated_at = now
        self.session.flush()
        return machine_from_row(row)

    def list_for_capacity_owner(self, workspace_id: str, capacity_owner_id: str) -> list[Machine]:
        statement = select(MachineTable).where(
            MachineTable.workspace_id == workspace_id,
            MachineTable.capacity_owner_id == capacity_owner_id,
        )
        return [machine_from_row(row) for row in self.session.scalars(statement)]


@dataclass(slots=True)
class WorkerRepository:
    session: Session

    def upsert(self, worker: Worker, *, workspace_id: str | None = None) -> Worker:
        worker = Worker.model_validate(dict(worker))
        row = self.session.get(WorkerTable, worker.id)
        owner_id = workspace_id if workspace_id is not None else row.workspace_id if row else None
        if owner_id is not None:
            WorkspaceRepository(self.session).lock_active_owner(owner_id)
        if row is None:
            row = WorkerTable(id=worker.id, workspace_id=owner_id, created_at=worker.created_at)
            self.session.add(row)
        elif row.workspace_id != owner_id:
            raise ConflictError("worker workspace cannot change")
        write_worker(row, worker)
        row.updated_at = utc_now()
        self.session.flush()
        return worker_from_row(row)

    def get(self, worker_id: str, *, workspace_id: str) -> Worker | None:
        if try_uuid(worker_id) is None:
            return None
        row = self.session.scalar(
            select(WorkerTable).where(
                WorkerTable.id == worker_id, WorkerTable.workspace_id == workspace_id
            )
        )
        return worker_from_row(row) if row is not None else None

    def get_across_workspaces(self, worker_id: str) -> Worker | None:
        if try_uuid(worker_id) is None:
            return None
        row = self.session.get(WorkerTable, worker_id)
        return worker_from_row(row) if row is not None else None

    def workspace_id(self, worker_id: str) -> str | None:
        return self.session.scalar(
            select(WorkerTable.workspace_id).where(WorkerTable.id == worker_id)
        )

    def list(self, *, workspace_id: str, status: str | None = None) -> list[Worker]:
        statement = select(WorkerTable).where(WorkerTable.workspace_id == workspace_id)
        if status is not None:
            statement = statement.where(WorkerTable.status == status)
        return [
            worker_from_row(row)
            for row in self.session.scalars(
                statement.order_by(WorkerTable.created_at.desc(), WorkerTable.id)
            )
        ]

    def list_across_workspaces(self, *, status: str | None = None) -> list[Worker]:
        statement = select(WorkerTable)
        if status is not None:
            statement = statement.where(WorkerTable.status == status)
        return [
            worker_from_row(row)
            for row in self.session.scalars(
                statement.order_by(WorkerTable.created_at.desc(), WorkerTable.id)
            )
        ]

    def delete_across_workspaces(self, worker_id: str) -> None:
        self.session.execute(delete(WorkerTable).where(WorkerTable.id == worker_id))
        self.session.flush()


@dataclass(frozen=True, slots=True)
class ContainerPageCursor:
    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class ContainerPage:
    data: list[ContainerRecord]
    next: ContainerPageCursor | None = None


@dataclass(frozen=True, slots=True)
class LiveContainer:
    id: str
    status: ContainerStatus
    started_at: datetime | None
    placed: bool
    """A worker has taken the container."""


@dataclass(frozen=True, slots=True)
class StartupFailure:
    container_id: str
    reason: str


def pinned_container() -> ColumnElement[bool]:
    """A container the platform may not move: it did not accept interruption, or it
    must run on one named worker. Every other container may be stopped and
    rescheduled elsewhere, as a preemption would."""
    return or_(
        ContainerTable.scheduling_preemptible.is_(False),
        ContainerTable.scheduling_required_worker_id != "",
    )


@dataclass(frozen=True, slots=True)
class MachineContainer:
    id: str
    workspace_id: str
    pinned: bool
    image_build: bool


@dataclass(frozen=True, slots=True)
class UnplacedPlatformDemand:
    preemptible_cpu: bool = False
    cpu: bool = False
    gpu_types: frozenset[str] = frozenset()
    """Every card an unplaced GPU request accepts."""

    @property
    def any(self) -> bool:
        return self.cpu or bool(self.gpu_types)


@dataclass(slots=True)
class ContainerRepository:
    session: Session

    def unplaced_platform_demand(self) -> UnplacedPlatformDemand:
        """Which platform markets have requests still waiting for a worker."""
        unplaced = and_(
            ContainerTable.status == ContainerStatus.Pending.value,
            ContainerTable.scheduling_requested_at.is_not(None),
            ContainerTable.runtime_worker_id == "",
            ContainerTable.scheduling_placement == Placement.platform().key,
        )
        cpu = self.session.execute(
            select(
                func.count().filter(ContainerTable.scheduling_preemptible.is_(True)),
                func.count(),
            ).where(unplaced, ContainerTable.scheduling_gpu_count == 0)
        ).one()
        cards = self.session.scalars(
            select(func.unnest(ContainerTable.scheduling_gpu))
            .where(unplaced, ContainerTable.scheduling_gpu_count > 0)
            .distinct()
        )
        return UnplacedPlatformDemand(
            preemptible_cpu=bool(cpu[0]),
            cpu=bool(cpu[1]),
            gpu_types=frozenset(str(card) for card in cards),
        )

    def has_live_for_placement(
        self,
        placement: str,
        *,
        worker_ids: Collection[str],
        workspace_ids: Collection[str],
    ) -> bool:
        """Whether live work was placed here, sits on these workers, or may still land here.

        Across every workspace a unit serves, because a container's own scheduling
        request is the record of where it was sent. A container in one of those
        workspaces that has no request and no worker yet may still be sent here,
        so it counts too.
        """
        ids = list(worker_ids)
        # `worker_id` is a UUID column; a scheduler worker id may be any string.
        physical_ids = [item for item in ids if try_uuid(item) is not None]
        unassigned = and_(
            ContainerTable.workspace_id.in_(list(workspace_ids)),
            ContainerTable.scheduling_placement.is_(None),
            ContainerTable.runtime_worker_id == "",
            ContainerTable.worker_id.is_(None),
        )
        on_worker = (
            or_(
                ContainerTable.runtime_worker_id.in_(ids),
                ContainerTable.worker_id.in_(physical_ids) if physical_ids else false(),
            )
            if ids
            else false()
        )
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        ContainerTable.status.in_(
                            (ContainerStatus.Pending.value, ContainerStatus.Running.value)
                        ),
                        or_(
                            ContainerTable.scheduling_placement == placement,
                            on_worker,
                            unassigned,
                        ),
                    )
                )
            )
        )

    def list_pending_storage_cleanup(self, worker_id: str) -> dict[str, StopContainerReason]:
        """Terminal containers on this worker whose storage is not yet released.

        Each maps to the reason recorded for its end, which a worker still
        running one needs in order to stop it the way the platform ended it.
        """
        runtime_worker_id = ContainerTable.runtime_worker_id
        assigned_worker = runtime_worker_id == worker_id
        physical_worker_id = try_uuid(worker_id)
        if physical_worker_id is not None:
            assigned_worker = or_(
                assigned_worker,
                and_(
                    or_(runtime_worker_id.is_(None), runtime_worker_id == ""),
                    ContainerTable.worker_id == physical_worker_id,
                ),
            )
        rows = self.session.execute(
            select(ContainerTable.id, ContainerTable.termination_reason)
            .where(
                ContainerTable.storage_released_at.is_(None),
                ContainerTable.status.not_in([status.value for status in LIVE_CONTAINER_STATUSES]),
                assigned_worker,
            )
            .order_by(ContainerTable.id)
        )
        return {container_id: StopContainerReason(reason) for container_id, reason in rows}

    def mark_storage_released(self, container_id: str, *, worker_id: str, now: datetime) -> None:
        row = self.session.scalar(
            select(ContainerTable).where(ContainerTable.id == container_id).with_for_update()
        )
        if row is None:
            return
        assigned_worker = container_from_row(row).runtime_worker_id
        if assigned_worker and assigned_worker != worker_id:
            raise ConflictError("container storage release belongs to another worker")
        if row.status in {status.value for status in LIVE_CONTAINER_STATUSES}:
            raise ConflictError("container must be stopped before releasing its storage")
        if row.storage_released_at is None:
            row.storage_released_at = now

    def storage_is_released(self, container_id: str, *, worker_id: str) -> bool:
        row = self.session.get(ContainerTable, container_id)
        if row is None or row.storage_released_at is None:
            return False
        record = container_from_row(row)
        return (record.runtime_worker_id or record.worker_id or "") == worker_id

    def lock_reservation(self, container_id: str) -> None:
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"container-reservation:{container_id}"},
        )

    def create(self, container: ContainerRecord) -> ContainerRecord:
        container = ContainerRecord.model_validate(dict(container))
        WorkspaceRepository(self.session).lock_active_owner(container.workspace_id)
        self._assert_admission(container)
        row = ContainerTable(id=container.id, created_at=container.created_at)
        write_container(row, container)
        self.session.add(row)
        self.session.flush()
        return container_from_row(row)

    def upsert(self, container: ContainerRecord) -> ContainerRecord:
        container = ContainerRecord.model_validate(dict(container))
        WorkspaceRepository(self.session).lock_active_owner(container.workspace_id)
        row = self.session.get(ContainerTable, container.id)
        if row is None:
            self._assert_admission(container)
            row = ContainerTable(id=container.id, created_at=container.created_at)
            self.session.add(row)
        elif row.workspace_id != container.workspace_id:
            raise ConflictError("container workspace cannot change")
        write_container(row, container)
        row.updated_at = utc_now()
        self.session.flush()
        return container_from_row(row)

    def _assert_admission(self, container: ContainerRecord) -> None:
        cleanup = CleanupRepository(self.session)
        if container.stub_id is not None:
            cleanup.assert_stub_available(container.stub_id)
        if container.image:
            cleanup.assert_references_available(
                workspace_id=container.workspace_id, object_ids=set(), image_ids={container.image}
            )

    def delete(self, container_id: str, *, workspace_id: str) -> bool:
        if try_uuid(container_id) is None:
            return False
        deleted = self.session.scalar(
            delete(ContainerTable)
            .where(ContainerTable.id == container_id, ContainerTable.workspace_id == workspace_id)
            .returning(ContainerTable.id)
        )
        return deleted is not None

    def stop_for_workspace_deletion(
        self,
        container: ContainerRecord,
        *,
        now: datetime,
    ) -> ContainerRecord:
        """Persist only an existing container's terminal deletion transition."""
        workspace = WorkspaceRepository(self.session).lock_for_deletion(container.workspace_id)
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(
                f"workspace cleanup requires deleting state: {container.workspace_id}"
            )
        current = self.get(container.id, workspace_id=container.workspace_id)
        if current is None:
            raise ConflictError(f"container disappeared during workspace deletion: {container.id}")
        stopped = current.model_copy(
            update={
                "status": ContainerStatus.Stopped,
                "finished_at": container.finished_at or now,
            }
        )
        row = self.session.get(ContainerTable, current.id)
        if row is None or str(row.workspace_id) != current.workspace_id:
            raise ConflictError(f"container disappeared during workspace deletion: {current.id}")
        row.status = ContainerStatus.Stopped.value
        row.finished_at = stopped.finished_at
        self.session.flush()
        return stopped

    def get(self, container_id: str, *, workspace_id: str) -> ContainerRecord | None:
        if try_uuid(container_id) is None:
            return None
        row = self.session.scalar(
            select(ContainerTable).where(
                ContainerTable.id == container_id, ContainerTable.workspace_id == workspace_id
            )
        )
        return container_from_row(row) if row is not None else None

    def get_across_workspaces(self, container_id: str) -> ContainerRecord | None:
        """System lookup for scheduler/worker/reconciler container control."""
        if try_uuid(container_id) is None:
            return None
        row = self.session.get(ContainerTable, container_id)
        return container_from_row(row) if row is not None else None

    def lock(self, container_id: str, *, workspace_id: str) -> ContainerRecord | None:
        row = self.session.scalar(
            select(ContainerTable)
            .where(ContainerTable.id == container_id, ContainerTable.workspace_id == workspace_id)
            .with_for_update()
        )
        return container_from_row(row) if row is not None else None

    def lock_across_workspaces(self, container_id: str) -> ContainerRecord | None:
        row = self.session.scalar(
            select(ContainerTable).where(ContainerTable.id == container_id).with_for_update()
        )
        return container_from_row(row) if row is not None else None

    def list_live_runtime_assignments(self, worker_id: str) -> list[ContainerRecord]:
        rows = self.session.scalars(
            select(ContainerTable)
            .where(
                ContainerTable.runtime_worker_id == worker_id,
                ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
            )
            .order_by(ContainerTable.id)
            .with_for_update()
        )
        return [container_from_row(row) for row in rows]

    def list(
        self,
        *,
        workspace_id: str,
        statuses: tuple[str, ...] = (),
        app_id: str | None = None,
        stub_ids: tuple[str, ...] = (),
    ) -> list[ContainerRecord]:
        return self._list(
            workspace_id=workspace_id,
            statuses=statuses,
            app_id=app_id,
            stub_ids=stub_ids,
        )

    def list_across_workspaces(
        self,
        *,
        statuses: tuple[str, ...] = (),
        app_id: str | None = None,
        stub_ids: tuple[str, ...] = (),
    ) -> list[ContainerRecord]:
        """System listing over every workspace's containers."""
        return self._list(
            workspace_id=None,
            statuses=statuses,
            app_id=app_id,
            stub_ids=stub_ids,
        )

    def any_with_status(
        self, statuses: tuple[ContainerStatus, ...], *, container_id: str | None = None
    ) -> bool:
        statement = exists().where(
            ContainerTable.status.in_(tuple(status.value for status in statuses))
        )
        if container_id is not None:
            statement = statement.where(ContainerTable.id == container_id)
        return bool(self.session.scalar(select(statement)))

    def progress_for_workloads(
        self, workloads: set[tuple[str, str]]
    ) -> list[ContainerProgressSnapshot]:
        if not workloads:
            return []
        rows = self.session.execute(
            select(
                ContainerTable.id,
                ContainerTable.workspace_id,
                ContainerTable.stub_id,
                ContainerTable.status,
                ContainerTable.worker_id,
            ).where(
                tuple_(ContainerTable.workspace_id, ContainerTable.stub_id).in_(workloads),
                ContainerTable.status.in_(
                    (ContainerStatus.Pending.value, ContainerStatus.Running.value)
                ),
            )
        )
        return [
            ContainerProgressSnapshot(
                row.id, row.workspace_id, row.stub_id, ContainerStatus(row.status), row.worker_id
            )
            for row in rows
        ]

    def count_live_cpu_for_owner(self, *, owner_user_id: str) -> int:
        """How many containers without a GPU this account is holding, everywhere.

        Counted across every workspace the account owns, because the limit is a
        term of a plan and a plan belongs to a payer. Counted per workspace it
        would be a limit anybody raises by making another workspace, which is
        self-serve.

        One statement rather than resolving the workspaces first: the set stays
        inside the query where the index on the membership rows can serve it, and
        the answer is one round trip on a path taken at every container start.

        Deliberately approximate under concurrency. Two starts racing both read
        the same count and both insert, so the ceiling can be overshot by roughly
        the number of simultaneous starts. Closing that would mean locking the
        account for the length of every container start — a per-account exclusive
        lock in the path of every autoscaler ramp — to protect a guardrail whose
        overshoot is a percent or two, self-corrects at the next start, and is
        metered and invoiced like anything else. This bounds blast radius; it is
        not a money invariant.
        """

        return int(
            self.session.scalar(
                self._live_for_owner(
                    select(func.count(ContainerTable.id)), owner_user_id=owner_user_id
                ).where(ContainerTable.gpu_count == 0)
            )
            or 0
        )

    def count_live_gpus_for_owner(self, *, owner_user_id: str) -> int:
        """How many GPU cards this account is holding, everywhere.

        Cards rather than containers, because a container may ask for several and
        cards are what the plan's GPU pool bounds. Same account scope and the same
        deliberate approximation as the CPU count.
        """

        return int(
            self.session.scalar(
                self._live_for_owner(
                    select(func.coalesce(func.sum(ContainerTable.gpu_count), 0)),
                    owner_user_id=owner_user_id,
                )
            )
            or 0
        )

    def live_gpu_containers_for_owner(self, *, owner_user_id: str) -> list[ContainerRecord]:
        """Every container holding a card for this account, with what it asked for.

        The requested GPU models are needed to assess the plan change.
        Few rows: bounded by the plan's GPU
        pool, and read when a plan change has to know what is still running.
        """

        statement = self._live_for_owner(select(ContainerTable), owner_user_id=owner_user_id).where(
            ContainerTable.gpu_count > 0
        )
        return [container_from_row(row) for row in self.session.scalars(statement)]

    @staticmethod
    def _live_for_owner[T](statement: Select[tuple[T]], *, owner_user_id: str) -> Select[tuple[T]]:
        return statement.join(
            WorkspaceMemberTable,
            WorkspaceMemberTable.workspace_id == ContainerTable.workspace_id,
        ).where(
            WorkspaceMemberTable.user_id == owner_user_id,
            WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
            ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
        )

    def live_counts_for_workspaces(
        self, *, workspace_ids: Sequence[str]
    ) -> dict[ContainerStatus, int]:
        """What these workspaces are holding right now, per live status.

        Restricted to the live statuses so the partial workspace index answers
        it. Counting every status instead would scan their whole history to
        report a figure about the present, and get slower every day they are
        used.

        Summed across the set rather than reported per workspace: the reader is
        an account, and which of its workspaces a running container sits in is a
        question the app breakdown answers rather than this one.

        A status none of them holds is absent rather than zero; the caller names
        the statuses it renders.
        """

        if not workspace_ids:
            return {}
        rows = self.session.execute(
            select(ContainerTable.status, func.count(ContainerTable.id))
            .where(
                ContainerTable.workspace_id.in_(workspace_ids),
                ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
            )
            .group_by(ContainerTable.status)
        ).all()
        return {ContainerStatus(row[0]): int(row[1]) for row in rows}

    def lock_stub_capacity(self, stub_id: str) -> None:
        """Serialize the starts competing for one stub's container ceiling.

        `count_live_for_stub` on its own is a read, and a ceiling read by two
        transactions at once is not a ceiling: a burst of six invocations each
        saw the same count and each started a container, so a limit of six held
        nine. Held for the transaction that both counts and inserts the
        reservation, so the count the decision was made on is the count the
        insert lands against.

        Scoped to the stub, which is what the ceiling is about — starts for
        other functions do not queue behind this one. SQLite admits a single
        writer at a time and needs no second mechanism.
        """

        if self.session.get_bind().dialect.name != "postgresql":
            return
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"stub-container-capacity:{stub_id}"},
        )

    def live_on_machine(self, machine_id: str) -> list[MachineContainer]:
        """The live containers a machine holds, and whether each may be moved."""
        rows = self.session.execute(
            select(
                ContainerTable.id,
                ContainerTable.workspace_id,
                pinned_container(),
                exists().where(ImageBuildAttemptTable.container_id == ContainerTable.id),
            )
            .where(
                or_(
                    ContainerTable.machine_id == machine_id,
                    ContainerTable.runtime_machine_id == machine_id,
                ),
                ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
            )
            .order_by(ContainerTable.id)
        ).tuples()
        return [
            MachineContainer(
                id=str(container_id),
                workspace_id=str(workspace_id),
                pinned=bool(pinned),
                image_build=bool(build),
            )
            for container_id, workspace_id, pinned, build in rows
        ]

    def count_live_for_machine(self, machine_id: str) -> int:
        """How much work would be taken away with this machine.

        Read before reclaiming one, so a machine that is running containers is
        not terminated on a readiness signal that says only that we have stopped
        hearing from it. The reclaim bounds how long this may hold: rows outlive
        the worker that owned them when nothing settles them.

        Platform assignments carry only the runtime machine identity; private
        assignments also bind the durable machine foreign key.
        """

        return int(
            self.session.scalar(
                select(func.count(ContainerTable.id)).where(
                    or_(
                        ContainerTable.machine_id == machine_id,
                        ContainerTable.runtime_machine_id == machine_id,
                    ),
                    ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
                )
            )
            or 0
        )

    def newest_live_for_stub(self, stub_id: str) -> LiveContainer | None:
        """The stub's newest live container, a running one ahead of any still pending."""
        row = self.session.execute(
            select(
                ContainerTable.id,
                ContainerTable.status,
                ContainerTable.started_at,
                ContainerTable.worker_id,
                ContainerTable.runtime_worker_id,
            )
            .where(
                ContainerTable.stub_id == stub_id,
                ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
            )
            .order_by(
                (ContainerTable.status == ContainerStatus.Running.value).desc(),
                ContainerTable.created_at.desc(),
                ContainerTable.id.desc(),
            )
            .limit(1)
        ).first()
        if row is None:
            return None
        container_id, status, started_at, worker_id, runtime_worker_id = row
        return LiveContainer(
            id=str(container_id),
            status=ContainerStatus(status),
            started_at=started_at,
            placed=worker_id is not None or bool(runtime_worker_id),
        )

    def recent_startup_failure(self, stub_id: str, *, since: datetime) -> StartupFailure | None:
        """The stub's newest container that failed to start since `since`, and why.

        Only a container that never ran counts: one that ran and then exited
        failed for some other reason, which a start did not cause.
        """
        row = self.session.execute(
            select(ContainerTable.id, ContainerTable.startup_error)
            .where(
                ContainerTable.stub_id == stub_id,
                ContainerTable.status == ContainerStatus.Failed.value,
                ContainerTable.started_at.is_(None),
                ContainerTable.finished_at >= since,
            )
            .order_by(ContainerTable.finished_at.desc(), ContainerTable.id.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        return StartupFailure(
            container_id=str(row[0]), reason=row[1] or "the container failed to start"
        )

    def count_live_for_stub(self, stub_id: str) -> int:
        """How many containers are already serving this stub, or about to.

        `Pending` counts for the same reason it does per account: a container
        that has been asked for will answer claims shortly, and starting another
        because it has not started yet is how a burst turns into a container per
        call — the arrangement pooling replaced.
        """

        return int(
            self.session.scalar(
                select(func.count(ContainerTable.id)).where(
                    ContainerTable.stub_id == stub_id,
                    ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
                    ~select(ContainerRolloutDrainTable.container_id)
                    .where(ContainerRolloutDrainTable.container_id == ContainerTable.id)
                    .exists(),
                )
            )
            or 0
        )

    def autoscaling_candidates(
        self,
        *,
        stub_ids: Sequence[str],
        failed_since: datetime,
    ) -> list[ContainerRecord]:
        """Live containers and recent startup failures for these stubs."""
        wanted = tuple(dict.fromkeys(stub_ids))
        if not wanted:
            return []
        live = select(ContainerTable).where(
            ContainerTable.stub_id.in_(wanted),
            ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
        )
        failed = select(ContainerTable).where(
            ContainerTable.stub_id.in_(wanted),
            ContainerTable.status == ContainerStatus.Failed.value,
            or_(
                ContainerTable.created_at >= failed_since,
                ContainerTable.finished_at >= failed_since,
            ),
        )
        candidates = union_all(live, failed).subquery()
        candidate = aliased(ContainerTable, candidates)
        statement = select(candidate).order_by(
            candidate.stub_id.asc(),
            candidate.created_at.desc(),
            candidate.id.desc(),
        )
        return [container_from_row(row) for row in self.session.scalars(statement)]

    def live_container_ids_for_owner(self, *, owner_user_id: str, limit: int) -> list[str]:
        """Which containers this account is holding capacity for, oldest first.

        Ids rather than records: the caller stops them, and building a full
        `ContainerRecord` for each would load fields nothing reads.

        Oldest first so a bounded pass makes progress in the same order every
        time. An account over the limit is stopped across as many passes as it
        takes, and taking them newest-first would leave the longest-running — and
        so the most expensive — container alive the longest.
        """

        rows = self.session.scalars(
            select(ContainerTable.id)
            .join(
                WorkspaceMemberTable,
                WorkspaceMemberTable.workspace_id == ContainerTable.workspace_id,
            )
            .where(
                WorkspaceMemberTable.user_id == owner_user_id,
                WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
                ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
            )
            .order_by(ContainerTable.created_at.asc())
            .limit(limit)
        ).all()
        return [str(row) for row in rows]

    def list_shutdown_targets(
        self,
        *,
        workspace_id: str,
    ) -> list[ContainerShutdownTarget]:
        containers = [
            container_from_row(row)
            for row in self.session.scalars(
                select(ContainerTable).where(
                    ContainerTable.workspace_id == workspace_id, container_storage_release_pending()
                )
            )
        ]
        return [
            ContainerShutdownTarget(
                container_id=container.id,
                worker_id=container.runtime_worker_id or container.worker_id or "",
            )
            for container in containers
        ]

    def _list(
        self,
        *,
        workspace_id: str | None,
        statuses: tuple[str, ...],
        app_id: str | None,
        stub_ids: tuple[str, ...],
    ) -> list[ContainerRecord]:
        statement = select(ContainerTable)
        if workspace_id is not None:
            statement = statement.where(ContainerTable.workspace_id == workspace_id)
        if statuses:
            statement = statement.where(ContainerTable.status.in_(statuses))
        if app_id is not None:
            statement = statement.where(ContainerTable.app_id == app_id)
        if stub_ids:
            statement = statement.where(ContainerTable.stub_id.in_(stub_ids))
        statement = statement.order_by(ContainerTable.created_at.desc(), ContainerTable.id.desc())
        return [container_from_row(row) for row in self.session.scalars(statement)]

    def unsettled_preemptions_across_workspaces(self, *, limit: int) -> list[ContainerRecord]:
        """System recovery input: preempted containers whose retry intent never settled.

        A crash between the terminal commit and the settle call leaves the intent durable
        here; oldest first so the longest-stranded task recovers first.
        """
        statement = (
            select(ContainerTable)
            .where(
                ContainerTable.termination_reason == StopContainerReason.Preempted.value,
                ContainerTable.preemption_settled_at.is_(None),
            )
            .order_by(ContainerTable.finished_at.asc(), ContainerTable.id.asc())
            .limit(limit)
        )
        return [container_from_row(row) for row in self.session.scalars(statement)]

    def mark_preemption_settled(
        self,
        container_id: str,
        *,
        now: datetime,
    ) -> ContainerRecord | None:
        """Record that a preempted container's retry intent has been resolved."""
        row = self.session.get(ContainerTable, container_id)
        if row is None:
            return None
        record = container_from_row(row)
        if record.preemption_settled_at is not None:
            return record
        settled = record.model_copy(update={"preemption_settled_at": now})
        row.preemption_settled_at = now
        self.session.flush()
        return settled

    def expired_containers_across_workspaces(
        self,
        *,
        now: datetime,
    ) -> list[ContainerRecord]:
        """System reaper input: live containers past their durable expiry."""
        statement = select(ContainerTable).where(
            ContainerTable.expires_at.is_not(None),
            ContainerTable.expires_at <= now,
            ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
        )
        statement = statement.order_by(ContainerTable.expires_at.asc(), ContainerTable.id.asc())
        return [container_from_row(row) for row in self.session.scalars(statement)]

    def get_for_stub(
        self,
        container_id: str,
        *,
        workspace_id: str,
        stub_id: str,
    ) -> ContainerRecord | None:
        row = self.session.scalars(
            select(ContainerTable).where(
                ContainerTable.id == container_id,
                ContainerTable.workspace_id == workspace_id,
                ContainerTable.stub_id == stub_id,
            )
        ).first()
        return container_from_row(row) if row is not None else None

    def latest_for_stubs(
        self,
        *,
        workspace_id: str,
        stub_ids: Sequence[str],
    ) -> dict[str, ContainerRecord]:
        wanted = tuple(dict.fromkeys(stub_ids))
        if not wanted:
            return {}
        rank = func.row_number().over(
            partition_by=ContainerTable.stub_id,
            order_by=(
                func.coalesce(ContainerTable.started_at, ContainerTable.created_at).desc(),
                ContainerTable.created_at.desc(),
                ContainerTable.id.desc(),
            ),
        )
        ranked = (
            select(
                ContainerTable.id.label("container_id"),
                ContainerTable.stub_id.label("stub_id"),
                rank.label("position"),
            )
            .where(
                ContainerTable.workspace_id == workspace_id,
                ContainerTable.stub_id.in_(wanted),
            )
            .subquery()
        )
        rows = self.session.execute(
            select(ContainerTable, ranked.c.stub_id)
            .join(ranked, ranked.c.container_id == ContainerTable.id)
            .where(ranked.c.position == 1)
        )
        return {
            str(stub_id): container_from_row(row) for row, stub_id in rows if stub_id is not None
        }

    def page(
        self,
        *,
        workspace_id: str,
        statuses: tuple[str, ...] = (),
        app_id: str | None = None,
        stub_ids: tuple[str, ...] = (),
        cursor: ContainerPageCursor | None = None,
        limit: int = 100,
    ) -> ContainerPage:
        """Read a stable descending page without offset drift."""
        page_limit = max(limit, 1)
        statement = select(ContainerTable).where(ContainerTable.workspace_id == workspace_id)
        if statuses:
            statement = statement.where(ContainerTable.status.in_(statuses))
        if app_id is not None:
            statement = statement.where(ContainerTable.app_id == app_id)
        if stub_ids:
            statement = statement.where(ContainerTable.stub_id.in_(stub_ids))
        if cursor is not None:
            statement = statement.where(
                or_(
                    ContainerTable.created_at < cursor.created_at,
                    and_(
                        ContainerTable.created_at == cursor.created_at,
                        ContainerTable.id < cursor.id,
                    ),
                )
            )
        rows = list(
            self.session.scalars(
                statement.order_by(
                    ContainerTable.created_at.desc(),
                    ContainerTable.id.desc(),
                ).limit(page_limit + 1)
            )
        )
        page_rows = rows[:page_limit]
        next_cursor = None
        if len(rows) > len(page_rows) and page_rows:
            last = page_rows[-1]
            created_at = (
                last.created_at
                if last.created_at.tzinfo is not None
                else last.created_at.replace(tzinfo=UTC)
            )
            next_cursor = ContainerPageCursor(created_at=created_at, id=str(last.id))
        return ContainerPage(
            data=[container_from_row(row) for row in page_rows],
            next=next_cursor,
        )

    def creation_times(
        self,
        *,
        workspace_id: str,
        stub_ids: tuple[str, ...],
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 10_000,
    ) -> list[datetime]:
        """SQL-windowed container creation timestamps for the given stubs.

        Every container creation is one cold start; rows come back bounded and
        in ascending time order for the caller to bucket.
        """
        if not stub_ids:
            return []
        statement = select(ContainerTable.created_at).where(
            ContainerTable.workspace_id == workspace_id,
            ContainerTable.stub_id.in_(stub_ids),
        )
        if start is not None:
            statement = statement.where(ContainerTable.created_at >= start)
        if end is not None:
            statement = statement.where(ContainerTable.created_at <= end)
        statement = statement.order_by(ContainerTable.created_at.desc()).limit(max(limit, 1))
        values = list(self.session.scalars(statement))
        values.reverse()
        return [
            value if value.tzinfo is not None else value.replace(tzinfo=UTC) for value in values
        ]


@dataclass(slots=True)
class AgentRepository:
    session: Session

    def upsert(self, agent: AgentRecord, *, workspace_id: str | None = None) -> AgentRecord:
        agent = AgentRecord.model_validate(dict(agent))
        row = self.session.get(AgentTable, agent.id)
        owner_id = workspace_id if workspace_id is not None else row.workspace_id if row else None
        if owner_id is not None:
            WorkspaceRepository(self.session).lock_active_owner(owner_id)
        if row is None:
            row = AgentTable(id=agent.id, workspace_id=owner_id, created_at=agent.created_at)
            self.session.add(row)
        elif row.workspace_id != owner_id:
            raise ConflictError("agent workspace cannot change")
        write_agent(row, agent)
        row.updated_at = utc_now()
        self.session.flush()
        return agent_from_row(row)

    def get(self, agent_id: str, *, workspace_id: str) -> AgentRecord | None:
        if try_uuid(agent_id) is None:
            return None
        row = self.session.scalar(
            select(AgentTable).where(
                AgentTable.id == agent_id, AgentTable.workspace_id == workspace_id
            )
        )
        return agent_from_row(row) if row is not None else None

    def get_across_workspaces(self, agent_id: str) -> AgentRecord | None:
        if try_uuid(agent_id) is None:
            return None
        row = self.session.get(AgentTable, agent_id)
        return agent_from_row(row) if row is not None else None

    def workspace_id(self, agent_id: str) -> str | None:
        return self.session.scalar(select(AgentTable.workspace_id).where(AgentTable.id == agent_id))

    def list(self, *, workspace_id: str, status: str | None = None) -> list[AgentRecord]:
        statement = select(AgentTable).where(AgentTable.workspace_id == workspace_id)
        if status is not None:
            statement = statement.where(AgentTable.status == status)
        return [
            agent_from_row(row)
            for row in self.session.scalars(
                statement.order_by(AgentTable.created_at.desc(), AgentTable.id)
            )
        ]

    def list_across_workspaces(self, *, status: str | None = None) -> list[AgentRecord]:
        statement = select(AgentTable)
        if status is not None:
            statement = statement.where(AgentTable.status == status)
        return [
            agent_from_row(row)
            for row in self.session.scalars(
                statement.order_by(AgentTable.created_at.desc(), AgentTable.id)
            )
        ]

    def delete(self, agent_id: str, *, workspace_id: str) -> None:
        self.session.execute(
            delete(AgentTable).where(
                AgentTable.id == agent_id, AgentTable.workspace_id == workspace_id
            )
        )
        self.session.flush()


@dataclass(slots=True)
class AgentLeaseRepository:
    session: Session

    def upsert(self, lease: AgentLease, *, workspace_id: str) -> AgentLease:
        lease = AgentLease.model_validate(dict(lease))
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        agent = self.session.scalar(
            select(AgentTable.id)
            .where(
                AgentTable.id == lease.agent_id,
                AgentTable.workspace_id == workspace_id,
            )
            .with_for_update(read=True, key_share=True)
        )
        if agent is None:
            raise ConflictError("agent lease requires an agent in its workspace")
        row = self.session.get(AgentLeaseTable, lease.id)
        if row is None:
            row = AgentLeaseTable(id=lease.id, created_at=lease.created_at)
            self.session.add(row)
        elif (row.agent_id, row.resource_type, row.resource_id) != (
            lease.agent_id,
            lease.resource_type,
            lease.resource_id,
        ):
            raise ConflictError("agent lease ownership cannot change")
        write_agent_lease(row, lease)
        row.updated_at = utc_now()
        self.session.flush()
        return agent_lease_from_row(row)

    def get(self, lease_id: str) -> AgentLease | None:
        if try_uuid(lease_id) is None:
            return None
        row = self.session.get(AgentLeaseTable, lease_id)
        return agent_lease_from_row(row) if row is not None else None

    def list_for_workspace(
        self, workspace_id: str, *, status: str | None = None
    ) -> list[AgentLease]:
        statement = (
            select(AgentLeaseTable)
            .join(AgentTable, AgentTable.id == AgentLeaseTable.agent_id)
            .where(AgentTable.workspace_id == workspace_id)
        )
        if status is not None:
            statement = statement.where(AgentLeaseTable.status == status)
        return [
            agent_lease_from_row(row)
            for row in self.session.scalars(
                statement.order_by(AgentLeaseTable.created_at.desc(), AgentLeaseTable.id)
            )
        ]
