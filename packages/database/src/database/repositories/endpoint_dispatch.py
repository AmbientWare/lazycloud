from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from database.records.endpoint_dispatch import (
    EndpointDispatchObservationRecord,
    EndpointDispatchStateRecord,
)
from database.tables.endpoint_dispatch import EndpointDispatchTable
from shared.errors import NotFoundError
from shared.timestamps import to_utc, to_utc_or_none
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

_ACTIVE_STATUSES = ("queued", "waiting-capacity", "inflight")


@dataclass(slots=True)
class EndpointDispatchRepository:
    session: Session

    def create(self, record: EndpointDispatchStateRecord) -> EndpointDispatchStateRecord:
        row = EndpointDispatchTable(**_values(record))
        self.session.add(row)
        self.session.flush()
        return _record(row)

    def update(self, record: EndpointDispatchStateRecord) -> EndpointDispatchStateRecord:
        row = self.session.scalars(
            update(EndpointDispatchTable)
            .where(EndpointDispatchTable.task_id == record.task_id)
            .values(**_mutable_values(record), updated_at=func.now())
            .returning(EndpointDispatchTable)
            .execution_options(synchronize_session=False)
        ).one_or_none()
        if row is None:
            raise NotFoundError(f"endpoint dispatch not found: {record.task_id}")
        self.session.flush()
        return _record(row)

    def get(self, task_id: str) -> EndpointDispatchStateRecord | None:
        row = self.session.get(EndpointDispatchTable, task_id)
        return _record(row) if row is not None else None

    def get_for_update(self, task_id: str) -> EndpointDispatchStateRecord | None:
        row = self.session.scalars(
            select(EndpointDispatchTable)
            .where(EndpointDispatchTable.task_id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        return _record(row) if row is not None else None

    def active_count(
        self,
        stub_id: str,
        *,
        at: datetime,
        exclude_task_id: str | None = None,
    ) -> int:
        statement = select(func.count(EndpointDispatchTable.task_id)).where(
            EndpointDispatchTable.stub_id == stub_id,
            EndpointDispatchTable.status.in_(_ACTIVE_STATUSES),
            EndpointDispatchTable.expires_at > at,
        )
        if exclude_task_id is not None:
            statement = statement.where(EndpointDispatchTable.task_id != exclude_task_id)
        return int(self.session.scalar(statement) or 0)

    def active_counts_by_stub(
        self,
        stub_ids: Sequence[str],
        *,
        at: datetime,
    ) -> dict[str, int]:
        wanted = tuple(dict.fromkeys(stub_ids))
        if not wanted:
            return {}
        rows = self.session.execute(
            select(
                EndpointDispatchTable.stub_id,
                func.count(EndpointDispatchTable.task_id),
            )
            .where(
                EndpointDispatchTable.stub_id.in_(wanted),
                EndpointDispatchTable.status.in_(_ACTIVE_STATUSES),
                EndpointDispatchTable.expires_at > at,
            )
            .group_by(EndpointDispatchTable.stub_id)
        )
        counts = {str(stub_id): int(count) for stub_id, count in rows}
        return {stub_id: counts.get(stub_id, 0) for stub_id in wanted}

    def inflight_counts(self, stub_id: str, *, at: datetime) -> dict[str, int]:
        rows = self.session.execute(
            select(
                EndpointDispatchTable.container_id,
                func.count(EndpointDispatchTable.task_id),
            )
            .where(
                EndpointDispatchTable.stub_id == stub_id,
                EndpointDispatchTable.status == "inflight",
                EndpointDispatchTable.expires_at > at,
                EndpointDispatchTable.container_id.is_not(None),
            )
            .group_by(EndpointDispatchTable.container_id)
        )
        return {
            str(container_id): int(count)
            for container_id, count in rows
            if container_id is not None
        }

    def observations_by_stub(
        self,
        stub_ids: Sequence[str],
        *,
        at: datetime,
        finished_since: datetime,
    ) -> dict[str, list[EndpointDispatchObservationRecord]]:
        wanted = tuple(dict.fromkeys(stub_ids))
        observations: dict[str, list[EndpointDispatchObservationRecord]] = {
            stub_id: [] for stub_id in wanted
        }
        if not wanted:
            return observations
        active = and_(
            EndpointDispatchTable.status == "inflight",
            EndpointDispatchTable.expires_at > at,
        )
        rows = self.session.execute(
            select(
                EndpointDispatchTable.stub_id,
                EndpointDispatchTable.container_id,
                func.count(EndpointDispatchTable.task_id).filter(active).label("active_count"),
                func.max(EndpointDispatchTable.finished_at).label("finished_at"),
            )
            .where(
                EndpointDispatchTable.stub_id.in_(wanted),
                EndpointDispatchTable.container_id.is_not(None),
                or_(
                    active,
                    EndpointDispatchTable.finished_at >= finished_since,
                ),
            )
            .group_by(
                EndpointDispatchTable.stub_id,
                EndpointDispatchTable.container_id,
            )
        )
        for stub_id, container_id, active_count, finished_at in rows:
            observations[str(stub_id)].append(
                EndpointDispatchObservationRecord(
                    stub_id=str(stub_id),
                    container_id=str(container_id),
                    active=bool(active_count),
                    finished_at=to_utc_or_none(finished_at),
                )
            )
        return observations


def _values(record: EndpointDispatchStateRecord) -> dict[str, object]:
    return {
        "task_id": record.task_id,
        "workspace_id": record.workspace_id,
        "stub_id": record.stub_id,
        "container_id": record.container_id,
        "method": record.method,
        "path": record.path,
        "status": record.status,
        "wait_timeout_seconds": record.wait_timeout_seconds,
        "max_pending_requests": record.max_pending_requests,
        "max_inflight_per_container": record.max_inflight_per_container,
        "attempts": record.attempts,
        "enqueued_at": record.enqueued_at,
        "started_at": record.started_at,
        "heartbeat_at": record.heartbeat_at,
        "expires_at": record.expires_at,
        "finished_at": record.finished_at,
        "error": record.error,
    }


def _mutable_values(record: EndpointDispatchStateRecord) -> dict[str, object]:
    return {
        "container_id": record.container_id,
        "status": record.status,
        "attempts": record.attempts,
        "enqueued_at": record.enqueued_at,
        "started_at": record.started_at,
        "heartbeat_at": record.heartbeat_at,
        "expires_at": record.expires_at,
        "finished_at": record.finished_at,
        "error": record.error,
    }


def _record(row: EndpointDispatchTable) -> EndpointDispatchStateRecord:
    return EndpointDispatchStateRecord(
        task_id=row.task_id,
        workspace_id=row.workspace_id,
        stub_id=row.stub_id,
        container_id=row.container_id,
        method=row.method,
        path=row.path,
        status=row.status,
        wait_timeout_seconds=row.wait_timeout_seconds,
        max_pending_requests=row.max_pending_requests,
        max_inflight_per_container=row.max_inflight_per_container,
        attempts=row.attempts,
        enqueued_at=to_utc(row.enqueued_at),
        started_at=to_utc_or_none(row.started_at),
        heartbeat_at=to_utc_or_none(row.heartbeat_at),
        expires_at=to_utc(row.expires_at),
        finished_at=to_utc_or_none(row.finished_at),
        error=row.error,
    )


__all__ = ["EndpointDispatchRepository"]
