from dataclasses import dataclass
from datetime import datetime

from database.tables.orchestration import ContainerTable
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import ConflictError
from shared.scheduling import SchedulerWorkerRequest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session


@dataclass(slots=True)
class ContainerSchedulingRepository:
    session: Session

    def submit(self, request: SchedulerWorkerRequest, *, now: datetime) -> bool:
        row = self.session.scalar(
            select(ContainerTable)
            .where(ContainerTable.id == request.container_id)
            .with_for_update()
        )
        if row is None or row.workspace_id != request.workspace_id:
            raise ConflictError("scheduling request does not own the container")
        if row.scheduling_request is not None:
            persisted = SchedulerWorkerRequest.model_validate(row.scheduling_request)
            transient = {"retry_count", "capacity_retry_at", "backfill", "timestamp"}
            if persisted.model_dump(exclude=transient) != request.model_dump(exclude=transient):
                raise ConflictError("a container's scheduling request cannot change")
            return row.status == ContainerStatus.Pending.value and not row.payload.get(
                "runtime_worker_id"
            )
        if row.status != ContainerStatus.Pending.value or row.payload.get("runtime_worker_id"):
            raise ConflictError("container is no longer awaiting scheduling")
        row.scheduling_request = request.model_dump(mode="json")
        row.scheduling_reconcile_at = now
        self.session.flush()
        return True

    def recoverable(self, *, now: datetime, limit: int) -> list[SchedulerWorkerRequest]:
        rows = self.session.scalars(
            select(ContainerTable.scheduling_request)
            .where(
                ContainerTable.scheduling_request.is_not(None),
                ContainerTable.scheduling_reconcile_at <= now,
                ContainerTable.status == ContainerStatus.Pending.value,
                ContainerTable.payload["runtime_worker_id"].as_string() == "",
            )
            .order_by(ContainerTable.scheduling_reconcile_at, ContainerTable.id)
            .limit(limit)
        )
        return [SchedulerWorkerRequest.model_validate(row) for row in rows]

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
            select(ContainerTable)
            .where(ContainerTable.id == request.container_id)
            .with_for_update()
        )
        if row is None or row.workspace_id != request.workspace_id:
            raise ConflictError("capacity demand does not own the container")
        if row.status != ContainerStatus.Pending.value or row.payload.get("runtime_worker_id"):
            raise ConflictError("container no longer needs capacity")
        if row.scheduling_request is None:
            row.scheduling_request = request.model_dump(mode="json")
            row.scheduling_reconcile_at = now
        if row.capacity_retry_at is None:
            row.capacity_retry_at = now
            self.session.flush()
        return SchedulerWorkerRequest.model_validate(row.scheduling_request)

    def capacity_due(self, *, now: datetime, limit: int) -> list[SchedulerWorkerRequest]:
        rows = self.session.scalars(
            select(ContainerTable.scheduling_request)
            .where(
                ContainerTable.scheduling_request.is_not(None),
                ContainerTable.capacity_retry_at <= now,
                ContainerTable.status == ContainerStatus.Pending.value,
                ContainerTable.payload["runtime_worker_id"].as_string() == "",
            )
            .order_by(ContainerTable.capacity_retry_at, ContainerTable.id)
            .limit(limit)
        )
        return [
            SchedulerWorkerRequest.model_validate(row).model_copy(update={"payload": {}})
            for row in rows
        ]

    def capacity_due_request(
        self, container_id: str, *, now: datetime
    ) -> SchedulerWorkerRequest | None:
        payload = self.session.scalar(
            select(ContainerTable.scheduling_request).where(
                ContainerTable.id == container_id,
                ContainerTable.capacity_retry_at <= now,
                ContainerTable.status == ContainerStatus.Pending.value,
                ContainerTable.payload["runtime_worker_id"].as_string() == "",
            )
        )
        return (
            SchedulerWorkerRequest.model_validate(payload).model_copy(update={"payload": {}})
            if payload is not None
            else None
        )

    def record_capacity_attempt(
        self, request: SchedulerWorkerRequest, *, retry_at: datetime
    ) -> None:
        row = self.session.scalar(
            select(ContainerTable)
            .where(
                ContainerTable.id == request.container_id,
                ContainerTable.workspace_id == request.workspace_id,
                ContainerTable.status == ContainerStatus.Pending.value,
                ContainerTable.payload["runtime_worker_id"].as_string() == "",
            )
            .with_for_update()
        )
        if row is None or row.scheduling_request is None:
            return
        durable = SchedulerWorkerRequest.model_validate(row.scheduling_request)
        row.scheduling_request = durable.model_copy(
            update={
                "retry_count": request.retry_count,
                "capacity_retry_at": retry_at,
            }
        ).model_dump(mode="json")
        row.capacity_retry_at = retry_at

    def assigned_at(self, container_id: str) -> datetime | None:
        return self.session.scalar(
            select(ContainerTable.scheduling_assigned_at).where(ContainerTable.id == container_id)
        )

    def request_for(self, container_id: str) -> SchedulerWorkerRequest | None:
        payload = self.session.scalar(
            select(ContainerTable.scheduling_request).where(ContainerTable.id == container_id)
        )
        return SchedulerWorkerRequest.model_validate(payload) if payload is not None else None

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
                ContainerTable.payload["runtime_worker_id"].as_string() != "",
                assigned_at <= before,
            )
            .order_by(assigned_at, ContainerTable.id)
            .limit(limit)
        )
        return [(ContainerRecord.model_validate(row.payload), timestamp) for row, timestamp in rows]

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
        row.payload = container.model_dump(mode="json")
        row.worker_id = container.worker_id
        row.machine_id = container.machine_id
        row.capacity_retry_at = None
        if row.scheduling_request is not None:
            row.scheduling_request = {**row.scheduling_request, "backfill": backfill}
        self.session.flush()
