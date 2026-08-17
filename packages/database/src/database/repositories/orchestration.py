from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from database.repositories.cleanup import CleanupRepository
from database.repositories.common import (
    GlobalTableRepository,
    TableRepositoryConfig,
    WorkspaceTableRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.tables.identity import WorkspaceMemberTable
from database.tables.orchestration import (
    AgentLeaseTable,
    AgentTable,
    AutoscalerStateTable,
    ContainerTable,
    MachineTable,
    RouteTable,
    WorkerTable,
)
from shared.autoscaler_state import (
    AutoscalerStateRecord,
    AutoscalerTargetKind,
    autoscaler_state_name,
)
from shared.compute_fleet import AgentLease, AgentRecord, Machine, ResourceStatus, Worker
from shared.container_requests import ContainerShutdownTarget, StopContainerReason
from shared.containers import LIVE_CONTAINER_STATUSES, ContainerRecord, ContainerStatus
from shared.errors import ConflictError
from shared.identity import WorkspaceRole, WorkspaceStatus
from shared.routing import AgentBackendRoute
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified


@dataclass(slots=True)
class AutoscalerStateRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[AutoscalerStateRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(
                AutoscalerStateTable,
                AutoscalerStateRecord,
                key_field="name",
            ),
        )

    def upsert(self, state: AutoscalerStateRecord) -> AutoscalerStateRecord:
        WorkspaceRepository(self.session).lock_active_owner(state.workspace_id)
        return self.records.upsert(
            state,
            key=state.name,
            workspace_id=state.workspace_id,
            name=state.name,
            status=state.decision,
        )

    def get(
        self,
        *,
        workspace_id: str,
        target_kind: AutoscalerTargetKind,
        target_id: str,
    ) -> AutoscalerStateRecord | None:
        return self.records.get(
            autoscaler_state_name(target_kind, target_id),
            workspace_id=workspace_id,
        )

    def list(
        self,
        *,
        workspace_id: str,
        source: str | None = None,
    ) -> list[AutoscalerStateRecord]:
        return _filtered_by_source(self.records.list(workspace_id=workspace_id), source)

    def list_across_workspaces(
        self,
        *,
        source: str | None = None,
    ) -> list[AutoscalerStateRecord]:
        """Scheduler-owned listing over every workspace's autoscaler targets."""
        return _filtered_by_source(self.records.list_across_workspaces(), source)


def _filtered_by_source(
    records: list[AutoscalerStateRecord],
    source: str | None,
) -> list[AutoscalerStateRecord]:
    if source is None:
        return records
    return [item for item in records if item.source == source]


@dataclass(slots=True)
class MachineRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[Machine]:
        return WorkspaceTableRepository(self.session, TableRepositoryConfig(MachineTable, Machine))

    def upsert(self, machine: Machine, *, workspace_id: str | None = None) -> Machine:
        """System-authority write; shared-pool machines carry no workspace."""
        return self.records.upsert_across_workspaces(
            machine,
            workspace_id=workspace_id,
            status=machine.status.value,
        )

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
        machine = Machine.model_validate(row.payload).model_copy(
            update={"status": ResourceStatus.Deleted}
        )
        row.payload = machine.model_dump(mode="json")
        row.status = ResourceStatus.Deleted.value
        flag_modified(row, "payload")
        self.session.flush()
        return machine

    def get(self, machine_id: str, *, workspace_id: str) -> Machine | None:
        return self.records.get(machine_id, workspace_id=workspace_id)

    def get_across_workspaces(self, machine_id: str) -> Machine | None:
        """System lookup for scheduler/provider reconcilers over the whole fleet."""
        return self.records.get_across_workspaces(machine_id)

    def workspace_id(self, machine_id: str) -> str | None:
        value = self.session.scalar(
            select(MachineTable.workspace_id).where(MachineTable.id == machine_id)
        )
        return str(value) if value is not None else None

    def list(self, *, workspace_id: str, status: str | None = None) -> list[Machine]:
        return self.records.list(workspace_id=workspace_id, status=status)

    def list_across_workspaces(self, *, status: str | None = None) -> list[Machine]:
        """System listing over the whole fleet, including unowned pool machines."""
        return self.records.list_across_workspaces(status=status)


@dataclass(slots=True)
class WorkerRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[Worker]:
        return WorkspaceTableRepository(self.session, TableRepositoryConfig(WorkerTable, Worker))

    def upsert(self, worker: Worker, *, workspace_id: str | None = None) -> Worker:
        """System-authority write; shared-pool workers carry no workspace."""
        return self.records.upsert_across_workspaces(
            worker,
            workspace_id=workspace_id,
            status=worker.status.value,
        )

    def get(self, worker_id: str, *, workspace_id: str) -> Worker | None:
        return self.records.get(worker_id, workspace_id=workspace_id)

    def get_across_workspaces(self, worker_id: str) -> Worker | None:
        """System lookup for the scheduler/worker fleet over every workspace."""
        return self.records.get_across_workspaces(worker_id)

    def workspace_id(self, worker_id: str) -> str | None:
        value = self.session.scalar(
            select(WorkerTable.workspace_id).where(WorkerTable.id == worker_id)
        )
        return str(value) if value is not None else None

    def list(self, *, workspace_id: str, status: str | None = None) -> list[Worker]:
        return self.records.list(workspace_id=workspace_id, status=status)

    def list_across_workspaces(self, *, status: str | None = None) -> list[Worker]:
        """System listing over the whole worker fleet."""
        return self.records.list_across_workspaces(status=status)


@dataclass(frozen=True, slots=True)
class ContainerPageCursor:
    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class ContainerPage:
    data: list[ContainerRecord]
    next: ContainerPageCursor | None = None


@dataclass(slots=True)
class ContainerRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ContainerRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(ContainerTable, ContainerRecord),
        )

    def upsert(self, container: ContainerRecord) -> ContainerRecord:
        cleanup = CleanupRepository(self.session)
        if container.stub_id is not None:
            cleanup.assert_stub_available(container.stub_id)
        if container.image:
            cleanup.assert_references_available(
                workspace_id=container.workspace_id,
                object_ids=set(),
                image_ids={container.image},
            )
        return self.records.upsert(
            container,
            workspace_id=container.workspace_id,
            name=container.name,
            status=container.status.value,
        )

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
        current = self.records.get(container.id, workspace_id=container.workspace_id)
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
        row.payload = stopped.model_dump(mode="json")
        row.status = ContainerStatus.Stopped.value
        row.finished_at = stopped.finished_at
        flag_modified(row, "payload")
        self.session.flush()
        return stopped

    def get(self, container_id: str, *, workspace_id: str) -> ContainerRecord | None:
        return self.records.get(container_id, workspace_id=workspace_id)

    def get_across_workspaces(self, container_id: str) -> ContainerRecord | None:
        """System lookup for scheduler/worker/reconciler container control."""
        return self.records.get_across_workspaces(container_id)

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

    def count_live_for_owner(self, *, owner_user_id: str) -> int:
        """How many containers this account is holding capacity for, everywhere.

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
                select(func.count(ContainerTable.id))
                .join(
                    WorkspaceMemberTable,
                    WorkspaceMemberTable.workspace_id == ContainerTable.workspace_id,
                )
                .where(
                    WorkspaceMemberTable.user_id == owner_user_id,
                    WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
                    ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
                )
            )
            or 0
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
                )
            )
            or 0
        )

    def live_container_ids_for_owner(self, *, owner_user_id: str, limit: int) -> list[str]:
        """Which containers this account is holding capacity for, oldest first.

        Ids rather than records: the caller stops them, and building a full
        `ContainerRecord` for each would validate a payload nothing reads.

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

    def list_active_shutdown_targets(
        self,
        *,
        workspace_id: str,
    ) -> list[ContainerShutdownTarget]:
        active = self.list(
            workspace_id=workspace_id,
            statuses=tuple(status.value for status in LIVE_CONTAINER_STATUSES),
        )
        return [
            ContainerShutdownTarget(
                container_id=container.id,
                worker_id=container.runtime_worker_id or container.worker_id or "",
            )
            for container in active
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
        return [
            ContainerRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

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
        return [
            ContainerRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

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
        record = ContainerRecord.model_validate(row.payload)
        if record.preemption_settled_at is not None:
            return record
        settled = record.model_copy(update={"preemption_settled_at": now})
        row.payload = settled.model_dump(mode="json")
        row.preemption_settled_at = now
        flag_modified(row, "payload")
        self.session.flush()
        return settled

    def expired_containers_across_workspaces(self, *, now: datetime) -> list[ContainerRecord]:
        """System reaper input: every live container past its expiry."""
        return [
            container
            for container in self.list_across_workspaces()
            if container.expires_at is not None
            and container.expires_at <= now
            and container.status in {ContainerStatus.Pending, ContainerStatus.Running}
        ]

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
            data=[ContainerRecord.model_validate(row.payload) for row in page_rows],
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

    @property
    def records(self) -> WorkspaceTableRepository[AgentRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(AgentTable, AgentRecord),
        )

    def upsert(self, agent: AgentRecord, *, workspace_id: str | None = None) -> AgentRecord:
        """System-authority write; cluster agents may carry no workspace."""
        return self.records.upsert_across_workspaces(
            agent,
            workspace_id=workspace_id,
            name=agent.name,
            status=agent.status.value,
        )

    def get(self, agent_id: str, *, workspace_id: str) -> AgentRecord | None:
        return self.records.get(agent_id, workspace_id=workspace_id)

    def get_across_workspaces(self, agent_id: str) -> AgentRecord | None:
        """System lookup over the whole agent fleet."""
        return self.records.get_across_workspaces(agent_id)

    def workspace_id(self, agent_id: str) -> str | None:
        value = self.session.scalar(
            select(AgentTable.workspace_id).where(AgentTable.id == agent_id)
        )
        return str(value) if value is not None else None

    def list(
        self,
        *,
        workspace_id: str,
        status: str | None = None,
    ) -> list[AgentRecord]:
        return self.records.list(workspace_id=workspace_id, status=status)

    def list_across_workspaces(self, *, status: str | None = None) -> list[AgentRecord]:
        """System listing over the whole agent fleet."""
        return self.records.list_across_workspaces(status=status)


@dataclass(slots=True)
class AgentLeaseRepository:
    session: Session

    @property
    def records(self) -> GlobalTableRepository[AgentLease]:
        return GlobalTableRepository(
            self.session,
            TableRepositoryConfig(AgentLeaseTable, AgentLease),
        )

    def upsert(self, lease: AgentLease) -> AgentLease:
        return self.records.upsert(lease, status=lease.status.value)

    def get(self, lease_id: str) -> AgentLease | None:
        return self.records.get(lease_id)

    def list(self, *, status: str | None = None) -> list[AgentLease]:
        return self.records.list(status=status)


@dataclass(slots=True)
class RouteRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[AgentBackendRoute]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(RouteTable, AgentBackendRoute, key_field="route_id"),
        )

    def upsert(self, route: AgentBackendRoute) -> AgentBackendRoute:
        return self.records.upsert(
            route,
            key=route.route_id,
            workspace_id=route.workspace_id,
            status=str(route.state),
        )

    def get(self, route_id: str, *, workspace_id: str) -> AgentBackendRoute | None:
        return self.records.get(route_id, workspace_id=workspace_id)

    def get_across_workspaces(self, route_id: str) -> AgentBackendRoute | None:
        """System lookup for request routing over every workspace's routes."""
        return self.records.get_across_workspaces(route_id)

    def list(self, *, workspace_id: str) -> list[AgentBackendRoute]:
        return self.records.list(workspace_id=workspace_id)

    def list_across_workspaces(self) -> list[AgentBackendRoute]:
        """System listing for route reconciliation."""
        return self.records.list_across_workspaces()
