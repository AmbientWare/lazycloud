from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from database.repositories.orchestration import AgentLeaseRepository, AgentRepository
from database.types import DatabaseSession
from foundation.ids import required_uuid
from observability.workspace_changes import WorkspaceChangePublisher
from shared.compute_fleet import AgentLease, AgentRecord, LeaseStatus, ResourceStatus
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.identity import WorkspaceRecord
from shared.placement import Placement
from shared.timestamps import utc_now

from database import DatabaseClient


class AgentContextPaths(Protocol):
    @property
    def root(self) -> Path: ...


class AgentContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...

    @property
    def paths(self) -> AgentContextPaths: ...

    def default_workspace_id(self, session: DatabaseSession) -> str: ...

    def workspace(
        self,
        session: DatabaseSession,
        workspace: str = "default",
    ) -> WorkspaceRecord: ...


class AgentService:
    def __init__(
        self,
        context: AgentContext,
        workspace_changes: WorkspaceChangePublisher | None = None,
    ) -> None:
        self.context = context
        self.workspace_changes = workspace_changes

    def register(
        self,
        name: str,
        *,
        placement: Placement = Placement.platform(),
        version: str = "local",
        capacity: dict[str, int | float | str] | None = None,
        labels: dict[str, str] | None = None,
        workspace: str = "default",
    ) -> AgentRecord:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            agent = AgentRepository(session).upsert(
                AgentRecord(
                    id=str(uuid4()),
                    name=name,
                    placement=placement,
                    version=version,
                    capacity=capacity or {},
                    labels=labels or {},
                    status=ResourceStatus.Running,
                    last_seen_at=utc_now(),
                ),
                workspace_id=workspace_id,
            )
        self._publish_change(
            workspace_id=workspace_id,
            change=WorkspaceChangeType.Created,
            resource_id=agent.id,
        )
        return agent

    def heartbeat(
        self,
        agent_id: str,
        *,
        capacity: dict[str, int | float | str] | None = None,
        workspace: str = "default",
    ) -> AgentRecord:
        with self.context.database.session() as session:
            repository = AgentRepository(session)
            workspace_id = self.context.workspace(session, workspace).id
            agent = repository.get(agent_id, workspace_id=workspace_id)
            if agent is None:
                msg = f"agent not found: {agent_id}"
                raise KeyError(msg)
            changed = agent.status is not ResourceStatus.Running or (
                capacity is not None and agent.capacity != capacity
            )
            agent.status = ResourceStatus.Running
            agent.last_seen_at = utc_now()
            agent.updated_at = utc_now()
            if capacity is not None:
                agent.capacity = capacity
            updated = repository.upsert(
                agent,
                workspace_id=workspace_id,
            )
        if changed:
            self._publish_change(
                workspace_id=workspace_id,
                change=WorkspaceChangeType.Updated,
                resource_id=agent_id,
            )
        return updated

    def list_agents(self, *, workspace: str = "default") -> list[AgentRecord]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            records = AgentRepository(session).list(workspace_id=workspace_id)
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def delete(self, agent_id: str, *, workspace: str = "default") -> None:
        with self.context.database.session() as session:
            agent_repository = AgentRepository(session)
            workspace_id = self.context.workspace(session, workspace).id
            if agent_repository.get(agent_id, workspace_id=workspace_id) is None:
                msg = f"agent not found: {agent_id}"
                raise KeyError(msg)
            agent_repository.delete(agent_id, workspace_id=workspace_id)
        self._publish_change(
            workspace_id=workspace_id,
            change=WorkspaceChangeType.Deleted,
            resource_id=agent_id,
        )

    def lease(
        self,
        agent_id: str,
        *,
        resource_type: str,
        resource_id: str,
        ttl_seconds: int = 300,
        workspace: str = "default",
    ) -> AgentLease:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            resolved_agent_id = required_uuid(agent_id, field="agent_id")
            repository = AgentRepository(session)
            if repository.get(resolved_agent_id, workspace_id=workspace_id) is None:
                msg = f"agent not found: {agent_id}"
                raise KeyError(msg)
            return AgentLeaseRepository(session).upsert(
                AgentLease(
                    id=str(uuid4()),
                    agent_id=resolved_agent_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    expires_at=utc_now() + timedelta(seconds=ttl_seconds),
                ),
                workspace_id=workspace_id,
            )

    def release(self, lease_id: str, *, workspace: str = "default") -> AgentLease:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = AgentLeaseRepository(session)
            lease = repository.get(lease_id)
            if (
                lease is None
                or AgentRepository(session).workspace_id(str(lease.agent_id)) != workspace_id
            ):
                msg = f"lease not found: {lease_id}"
                raise KeyError(msg)
            lease.status = LeaseStatus.Released
            lease.released_at = utc_now()
            return repository.upsert(lease, workspace_id=workspace_id)

    def list_leases(
        self,
        *,
        workspace: str = "default",
        include_inactive: bool = False,
    ) -> list[AgentLease]:
        now = utc_now()
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = AgentLeaseRepository(session)
            records: list[AgentLease] = []
            for record in repository.list_for_workspace(
                workspace_id,
                status=None if include_inactive else LeaseStatus.Active.value,
            ):
                if record.status is LeaseStatus.Active and record.expires_at <= now:
                    record.status = LeaseStatus.Expired
                    record = repository.upsert(record, workspace_id=workspace_id)
                if include_inactive or record.status is LeaseStatus.Active:
                    records.append(record)
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def _publish_change(
        self,
        *,
        workspace_id: str,
        change: WorkspaceChangeType,
        resource_id: str,
    ) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputeAgents,
            change=change,
            resource_id=resource_id,
        )
