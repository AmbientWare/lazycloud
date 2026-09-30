from dataclasses import dataclass
from datetime import datetime, timedelta

from database.tables.apps import StubTable
from database.tables.orchestration import ContainerTable
from pydantic import BaseModel
from shared.containers import ContainerStatus
from shared.deployments import StubKind
from sqlalchemy import and_, case, func, select, true
from sqlalchemy.orm import Session


class SandboxObservation(BaseModel):
    id: str
    name: str
    created_at: datetime
    gpu: list[str] | None
    configured_status: str | None
    container_id: str | None
    container_status: ContainerStatus | None
    started_at: datetime | None
    finished_at: datetime | None


class SandboxBucket(BaseModel):
    day: datetime
    configured_status: str | None
    container_status: ContainerStatus | None
    count: int
    recent_count: int


@dataclass(slots=True)
class SandboxRepository:
    session: Session

    def statistics(
        self, *, workspace_id: str, app_id: str | None, now: datetime
    ) -> list[SandboxBucket]:
        created = func.coalesce(ContainerTable.created_at, StubTable.created_at)
        day = func.date_trunc("day", created, "UTC")
        configured = case(
            (ContainerTable.id.is_(None), StubTable.configuration["status"].as_string()), else_=None
        )
        statement = (
            select(
                day.label("day"),
                configured.label("configured_status"),
                ContainerTable.status.label("container_status"),
                func.count().label("count"),
                func.count()
                .filter(and_(created >= now - timedelta(days=1), created <= now))
                .label("recent_count"),
            )
            .select_from(StubTable)
            .outerjoin(
                ContainerTable,
                and_(
                    ContainerTable.stub_id == StubTable.id,
                    ContainerTable.workspace_id == workspace_id,
                ),
            )
            .where(StubTable.workspace_id == workspace_id, StubTable.type == StubKind.Sandbox.value)
            .group_by(day, configured, ContainerTable.status)
            .order_by(day)
        )
        if app_id is not None:
            statement = statement.where(StubTable.app_id == app_id)
        return [
            SandboxBucket.model_validate(row._mapping) for row in self.session.execute(statement)
        ]

    def list(
        self, *, workspace_id: str, app_id: str | None, limit: int
    ) -> list[SandboxObservation]:
        statement = select(
            StubTable.id,
            StubTable.name,
            StubTable.created_at,
            StubTable.runtime_gpu.label("gpu"),
            StubTable.configuration["status"].as_string().label("configured_status"),
        ).where(StubTable.workspace_id == workspace_id, StubTable.type == StubKind.Sandbox.value)
        if app_id is not None:
            statement = statement.where(StubTable.app_id == app_id)
        stubs = (
            statement.order_by(StubTable.created_at.desc(), StubTable.name, StubTable.id)
            .limit(limit)
            .subquery()
        )
        latest = (
            select(
                ContainerTable.id.label("container_id"),
                ContainerTable.status.label("container_status"),
                ContainerTable.started_at,
                ContainerTable.finished_at,
            )
            .where(
                ContainerTable.workspace_id == workspace_id, ContainerTable.stub_id == stubs.c.id
            )
            .order_by(
                func.coalesce(ContainerTable.started_at, ContainerTable.created_at).desc(),
                ContainerTable.created_at.desc(),
                ContainerTable.id.desc(),
            )
            .limit(1)
            .lateral()
        )
        rows = self.session.execute(
            select(stubs, latest)
            .outerjoin(latest, true())
            .order_by(stubs.c.created_at.desc(), stubs.c.name, stubs.c.id)
        )
        return [SandboxObservation.model_validate(row._mapping) for row in rows]
