from dataclasses import dataclass
from datetime import datetime

from database.mappers.containers import (
    container_from_row,
    scheduling_request_from_row,
    write_container,
    write_scheduling_request,
)
from database.tables.orchestration import ContainerTable
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import ConflictError
from shared.scheduling import SchedulerWorkerRequest
from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session, load_only, undefer


def _request_statement(*, include_payload: bool = True) -> Select[tuple[ContainerTable]]:
    projection = load_only(
        ContainerTable.capacity_retry_at,
        ContainerTable.id,
        ContainerTable.scheduling_architecture,
        ContainerTable.scheduling_availability_zone,
        ContainerTable.scheduling_backfill,
        ContainerTable.scheduling_cpu_millicores,
        ContainerTable.scheduling_deployment_id,
        ContainerTable.scheduling_disk_bytes,
        ContainerTable.scheduling_docker_enabled,
        ContainerTable.scheduling_gpu,
        ContainerTable.scheduling_gpu_count,
        ContainerTable.scheduling_memory_mib,
        ContainerTable.scheduling_placement,
        ContainerTable.scheduling_preemptible,
        ContainerTable.scheduling_provider_runtime,
        ContainerTable.scheduling_region,
        ContainerTable.scheduling_requested_at,
        ContainerTable.scheduling_preferred_worker_id,
        ContainerTable.scheduling_required_worker_id,
        ContainerTable.scheduling_retry_count,
        ContainerTable.scheduling_runtime_class,
        ContainerTable.scheduling_stub_id,
        ContainerTable.scheduling_workspace_cpu_quota_millicores,
        ContainerTable.scheduling_workspace_gpu_quota,
        ContainerTable.workspace_id,
        ContainerTable.status,
        ContainerTable.runtime_worker_id,
        raiseload=True,
    )
    statement = select(ContainerTable).options(projection)
    if include_payload:
        statement = statement.options(undefer(ContainerTable.scheduling_payload))
    return statement


@dataclass(slots=True)
class ContainerSchedulingRepository:
    session: Session

    def submit(self, request: SchedulerWorkerRequest, *, now: datetime) -> bool:
        row = self.session.scalar(
            _request_statement().where(ContainerTable.id == request.container_id).with_for_update()
        )
        if row is None or row.workspace_id != request.workspace_id:
            raise ConflictError("scheduling request does not own the container")
        if row.scheduling_requested_at is not None:
            persisted = scheduling_request_from_row(row)
            transient = {"retry_count", "capacity_retry_at", "backfill", "timestamp"}
            if persisted.model_dump(exclude=transient) != request.model_dump(exclude=transient):
                raise ConflictError("a container's scheduling request cannot change")
            return row.status == ContainerStatus.Pending.value and not row.runtime_worker_id
        if row.status != ContainerStatus.Pending.value or row.runtime_worker_id:
            raise ConflictError("container is no longer awaiting scheduling")
        write_scheduling_request(row, request)
        row.scheduling_reconcile_at = now
        self.session.flush()
        return True

    def recoverable(self, *, now: datetime, limit: int) -> list[SchedulerWorkerRequest]:
        rows = self.session.scalars(
            _request_statement()
            .where(
                ContainerTable.scheduling_requested_at.is_not(None),
                ContainerTable.scheduling_reconcile_at <= now,
                ContainerTable.status == ContainerStatus.Pending.value,
                ContainerTable.runtime_worker_id == "",
            )
            .order_by(ContainerTable.scheduling_reconcile_at, ContainerTable.id)
            .limit(limit)
        )
        return [scheduling_request_from_row(row) for row in rows]

    def reconciled(self, container_id: str, *, retry_at: datetime) -> None:
        self.session.execute(
            update(ContainerTable)
            .where(ContainerTable.id == container_id)
            .values(scheduling_reconcile_at=retry_at)
        )

    def request_capacity(
        self, request: SchedulerWorkerRequest, *, now: datetime
    ) -> SchedulerWorkerRequest:
        row = self.session.scalar(
            _request_statement().where(ContainerTable.id == request.container_id).with_for_update()
        )
        if row is None or row.workspace_id != request.workspace_id:
            raise ConflictError("capacity demand does not own the container")
        if row.status != ContainerStatus.Pending.value or row.runtime_worker_id:
            raise ConflictError("container no longer needs capacity")
        if row.scheduling_requested_at is None:
            write_scheduling_request(row, request)
            row.scheduling_reconcile_at = now
        if row.capacity_retry_at is None:
            row.capacity_retry_at = now
            self.session.flush()
        return scheduling_request_from_row(row)

    def capacity_due(self, *, now: datetime, limit: int) -> list[SchedulerWorkerRequest]:
        rows = self.session.scalars(
            _request_statement(include_payload=False)
            .where(
                ContainerTable.scheduling_requested_at.is_not(None),
                ContainerTable.capacity_retry_at <= now,
                ContainerTable.status == ContainerStatus.Pending.value,
                ContainerTable.runtime_worker_id == "",
            )
            .order_by(ContainerTable.capacity_retry_at, ContainerTable.id)
            .limit(limit)
        )
        return [scheduling_request_from_row(row, include_payload=False) for row in rows]

    def capacity_due_request(
        self, container_id: str, *, now: datetime
    ) -> SchedulerWorkerRequest | None:
        row = self.session.scalar(
            _request_statement(include_payload=False).where(
                ContainerTable.id == container_id,
                ContainerTable.scheduling_requested_at.is_not(None),
                ContainerTable.capacity_retry_at <= now,
                ContainerTable.status == ContainerStatus.Pending.value,
                ContainerTable.runtime_worker_id == "",
            )
        )
        return scheduling_request_from_row(row, include_payload=False) if row is not None else None

    def record_capacity_attempt(
        self, request: SchedulerWorkerRequest, *, retry_at: datetime
    ) -> None:
        row = self.session.scalar(
            select(ContainerTable)
            .where(
                ContainerTable.id == request.container_id,
                ContainerTable.workspace_id == request.workspace_id,
                ContainerTable.status == ContainerStatus.Pending.value,
                ContainerTable.runtime_worker_id == "",
            )
            .with_for_update()
        )
        if row is None or row.scheduling_requested_at is None:
            return
        row.scheduling_retry_count = request.retry_count
        row.capacity_retry_at = retry_at

    def assigned_at(self, container_id: str) -> datetime | None:
        return self.session.scalar(
            select(ContainerTable.scheduling_assigned_at).where(ContainerTable.id == container_id)
        )

    def request_for(self, container_id: str) -> SchedulerWorkerRequest | None:
        row = self.session.scalar(
            _request_statement().where(
                ContainerTable.id == container_id,
                ContainerTable.scheduling_requested_at.is_not(None),
            )
        )
        return scheduling_request_from_row(row) if row is not None else None

    def expired_assignments(
        self, *, before: datetime, limit: int
    ) -> list[tuple[ContainerRecord, datetime]]:
        assigned_at = func.coalesce(
            ContainerTable.scheduling_assigned_at, ContainerTable.created_at
        )
        rows = self.session.execute(
            select(ContainerTable, assigned_at)
            .where(
                ContainerTable.status == ContainerStatus.Pending.value,
                ContainerTable.runtime_worker_id != "",
                assigned_at <= before,
            )
            .order_by(assigned_at, ContainerTable.id)
            .limit(limit)
        )
        return [(container_from_row(row), timestamp) for row, timestamp in rows]

    def owns_assignment(self, container_id: str, *, token: str) -> bool:
        return (
            self.session.scalar(
                select(ContainerTable.scheduling_assignment_token).where(
                    ContainerTable.id == container_id
                )
            )
            == token
        )

    def record_assignment(
        self, container: ContainerRecord, *, now: datetime | None, token: str | None, backfill: bool
    ) -> None:
        row = self.session.get(ContainerTable, container.id)
        if row is None:
            raise ConflictError("assignment container is unavailable")
        row.scheduling_assigned_at = now
        row.scheduling_assignment_token = token
        if row.workspace_id != container.workspace_id:
            raise ConflictError("assignment does not own the container")
        write_container(row, container)
        row.capacity_retry_at = None
        if row.scheduling_requested_at is not None:
            row.scheduling_backfill = backfill
        self.session.flush()
