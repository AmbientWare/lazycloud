from __future__ import annotations

from shared.compute_fleet import AgentLease, AgentRecord, Machine, Worker
from shared.timestamps import to_utc, to_utc_or_none

from database.tables.orchestration import (
    AgentLeaseTable,
    AgentTable,
    MachineTable,
    MachineWorkspaceTable,
    WorkerTable,
)


def machine_from_row(row: MachineTable) -> Machine:
    return Machine.model_validate(
        {
            "id": row.id,
            "name": row.name or "",
            "workspace_ids": tuple(link.workspace_id for link in row.workspaces),
            "pool": row.pool,
            "capacity_owner_id": row.capacity_owner_id,
            "provider": row.provider,
            "status": row.status,
            "cpu": row.cpu,
            "memory": row.memory,
            "gpu": row.gpu,
            "gpu_count": row.gpu_count,
            "address": row.address,
            "labels": row.labels,
            "created_at": to_utc(row.created_at),
            "updated_at": to_utc(row.updated_at),
        }
    )


def write_machine(row: MachineTable, record: Machine) -> None:
    row.name = record.name or None
    current_links = {link.workspace_id: link for link in row.workspaces}
    row.workspaces = [
        current_links.get(workspace_id) or MachineWorkspaceTable(workspace_id=workspace_id)
        for workspace_id in dict.fromkeys(record.workspace_ids)
    ]
    row.pool = record.pool
    row.capacity_owner_id = record.capacity_owner_id
    row.provider = record.provider
    row.status = record.status.value
    row.cpu = record.cpu
    row.memory = record.memory
    row.gpu = record.gpu
    row.gpu_count = record.gpu_count
    row.address = record.address
    row.labels = dict(record.labels)


def worker_from_row(row: WorkerTable) -> Worker:
    return Worker.model_validate(
        {
            "id": row.id,
            "machine_id": row.machine_id,
            "pool": row.pool,
            "status": row.status,
            "labels": row.labels,
            "last_seen_at": to_utc(row.last_seen_at),
            "created_at": to_utc(row.created_at),
        }
    )


def write_worker(row: WorkerTable, record: Worker) -> None:
    row.machine_id = record.machine_id
    row.pool = record.pool
    row.status = record.status.value
    row.labels = dict(record.labels)
    row.last_seen_at = record.last_seen_at


def agent_from_row(row: AgentTable) -> AgentRecord:
    return AgentRecord.model_validate(
        {
            "id": row.id,
            "name": row.name,
            "pool": row.pool,
            "status": row.status,
            "version": row.version,
            "capacity": row.capacity,
            "labels": row.labels,
            "install_command": row.install_command,
            "last_seen_at": to_utc_or_none(row.last_seen_at),
            "created_at": to_utc(row.created_at),
            "updated_at": to_utc(row.updated_at),
        }
    )


def write_agent(row: AgentTable, record: AgentRecord) -> None:
    row.name = record.name
    row.pool = record.pool
    row.status = record.status.value
    row.version = record.version
    row.capacity = dict(record.capacity)
    row.labels = dict(record.labels)
    row.install_command = record.install_command
    row.last_seen_at = record.last_seen_at


def agent_lease_from_row(row: AgentLeaseTable) -> AgentLease:
    return AgentLease.model_validate(
        {
            "id": row.id,
            "agent_id": row.agent_id,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "status": row.status,
            "expires_at": to_utc(row.expires_at),
            "created_at": to_utc(row.created_at),
            "released_at": to_utc_or_none(row.released_at),
        }
    )


def write_agent_lease(row: AgentLeaseTable, record: AgentLease) -> None:
    row.agent_id = record.agent_id
    row.resource_type = record.resource_type
    row.resource_id = record.resource_id
    row.status = record.status.value
    row.expires_at = record.expires_at
    row.released_at = record.released_at
