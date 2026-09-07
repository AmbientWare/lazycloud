from __future__ import annotations

from dataclasses import dataclass

from database.tables.apps import AppTable, DeploymentTable, StubTable
from database.tables.execution import TaskTable
from database.tables.orchestration import ContainerTable
from pydantic import BaseModel
from shared.deployments import DeploymentKind
from shared.http.search import ResourceSearchKind
from sqlalchemy import String, cast, literal, or_, select, tuple_, union_all
from sqlalchemy.orm import Session


class ResourceSearchRecord(BaseModel):
    kind: ResourceSearchKind
    id: str
    name: str
    app_id: str | None
    workload_kind: DeploymentKind | None


@dataclass(slots=True)
class ResourceSearchRepository:
    session: Session

    def search(
        self,
        workspace_id: str,
        query: str,
        *,
        after: tuple[str, str, str] | None,
        limit: int,
    ) -> list[ResourceSearchRecord]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        apps = select(
            literal("app").label("kind"),
            cast(AppTable.id, String).label("id"),
            AppTable.name.label("name"),
            literal(None, String).label("app_id"),
            literal(None, String).label("workload_kind"),
        ).where(
            AppTable.workspace_id == workspace_id,
            AppTable.deleted_at.is_(None),
            or_(
                AppTable.name.ilike(pattern, escape="\\"),
                cast(AppTable.id, String).ilike(pattern, escape="\\"),
            ),
        )
        workloads = (
            select(
                literal("workload").label("kind"),
                (
                    cast(DeploymentTable.app_id, String)
                    + literal("/")
                    + DeploymentTable.kind
                    + literal("/")
                    + DeploymentTable.name
                ).label("id"),
                DeploymentTable.name.label("name"),
                cast(DeploymentTable.app_id, String).label("app_id"),
                DeploymentTable.kind.label("workload_kind"),
            )
            .join(AppTable, AppTable.id == DeploymentTable.app_id)
            .join(StubTable, StubTable.id == DeploymentTable.stub_id)
            .where(
                DeploymentTable.workspace_id == workspace_id,
                AppTable.workspace_id == workspace_id,
                StubTable.workspace_id == workspace_id,
                AppTable.deleted_at.is_(None),
                DeploymentTable.deleted_at.is_(None),
                or_(
                    DeploymentTable.name.ilike(pattern, escape="\\"),
                    cast(StubTable.id, String).ilike(pattern, escape="\\"),
                    StubTable.payload["handler"].as_string().ilike(pattern, escape="\\"),
                ),
            )
            .distinct()
        )
        tasks = select(
            literal("task").label("kind"),
            cast(TaskTable.id, String).label("id"),
            TaskTable.name.label("name"),
            cast(TaskTable.app_id, String).label("app_id"),
            literal(None, String).label("workload_kind"),
        ).where(
            TaskTable.workspace_id == workspace_id,
            or_(
                TaskTable.name.ilike(pattern, escape="\\"),
                cast(TaskTable.id, String).ilike(pattern, escape="\\"),
            ),
        )
        sandboxes = (
            select(
                literal("sandbox").label("kind"),
                cast(ContainerTable.id, String).label("id"),
                ContainerTable.name.label("name"),
                cast(ContainerTable.app_id, String).label("app_id"),
                literal(None, String).label("workload_kind"),
            )
            .join(StubTable, StubTable.id == ContainerTable.stub_id)
            .where(
                ContainerTable.workspace_id == workspace_id,
                StubTable.workspace_id == workspace_id,
                StubTable.type == "sandbox",
                or_(
                    ContainerTable.name.ilike(pattern, escape="\\"),
                    StubTable.name.ilike(pattern, escape="\\"),
                    cast(ContainerTable.id, String).ilike(pattern, escape="\\"),
                    cast(StubTable.id, String).ilike(pattern, escape="\\"),
                ),
            )
        )
        resources = union_all(apps, workloads, tasks, sandboxes).subquery()
        statement = select(resources)
        if after is not None:
            statement = statement.where(
                tuple_(resources.c.kind, resources.c.name, resources.c.id) > after
            )
        statement = statement.order_by(resources.c.kind, resources.c.name, resources.c.id).limit(
            limit
        )
        return [
            ResourceSearchRecord.model_validate(row)
            for row in self.session.execute(statement).mappings()
        ]
