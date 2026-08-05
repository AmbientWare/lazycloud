from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Protocol

from database.repositories.orchestration import AgentLeaseRepository, AgentRepository
from database.types import DatabaseSession
from foundation.ids import required_uuid
from observability.workspace_changes import WorkspaceChangePublisher
from shared.app_identity import ADMIN_CLI_NAME
from shared.compute_fleet import AgentLease, AgentRecord, LeaseStatus, ResourceStatus
from shared.compute_policy import MachinePool
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.identity import WorkspaceRecord
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
        pool: MachinePool = MachinePool("default"),
        version: str = "local",
        capacity: dict[str, int | float | str] | None = None,
        labels: dict[str, str] | None = None,
        workspace: str = "default",
    ) -> AgentRecord:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            agent = AgentRepository(session).records.create(
                {
                    "name": name,
                    "pool": pool,
                    "version": version,
                    "capacity": capacity or {},
                    "labels": labels or {},
                    "status": ResourceStatus.Running.value,
                    "last_seen_at": utc_now().isoformat(),
                    "install_command": f"{ADMIN_CLI_NAME} agent join --name {name} --pool {pool}",
                },
                workspace_id=workspace_id,
                name=name,
                status=ResourceStatus.Running.value,
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
            updated = repository.records.upsert(
                agent,
                workspace_id=workspace_id,
                name=agent.name,
                status=agent.status.value,
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
            leases = AgentLeaseRepository(session)
            for lease in leases.list():
                if lease.agent_id == agent_id and lease.status is LeaseStatus.Active:
                    lease.status = LeaseStatus.Released
                    lease.released_at = utc_now()
                    leases.upsert(lease)
            agent_repository.records.delete(agent_id, workspace_id=workspace_id)
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
            return AgentLeaseRepository(session).records.create(
                {
                    "agent_id": resolved_agent_id,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "status": LeaseStatus.Active.value,
                    "expires_at": (utc_now() + timedelta(seconds=ttl_seconds)).isoformat(),
                },
                status=LeaseStatus.Active.value,
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
            return repository.upsert(lease)

    def list_leases(
        self,
        *,
        workspace: str = "default",
        include_inactive: bool = False,
    ) -> list[AgentLease]:
        now = utc_now()
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            agent_ids = {
                agent.id for agent in AgentRepository(session).list(workspace_id=workspace_id)
            }
            repository = AgentLeaseRepository(session)
            records: list[AgentLease] = []
            for record in repository.records.list():
                if str(record.agent_id) not in agent_ids:
                    continue
                if record.status is LeaseStatus.Active and record.expires_at <= now:
                    record.status = LeaseStatus.Expired
                    record = repository.upsert(record)
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
