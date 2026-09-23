from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from database.repositories.orchestration import container_storage_release_pending
from database.tables.apps import DeploymentTable, StubTable
from database.tables.deployment_prunes import DeploymentPruneTable, DeploymentPruneTargetTable
from database.tables.orchestration import ContainerTable
from shared.container_requests import ContainerShutdownTarget
from shared.deployments import DeploymentKind
from sqlalchemy import String, cast, func, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class WorkloadSummary:
    kind: DeploymentKind
    name: str
    versions: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PruneOperation:
    id: str
    workspace_id: str
    app_id: str
    request_digest: str
    created_at: datetime
    complete: bool


@dataclass(slots=True)
class DeploymentPlanRepository:
    session: Session

    def summaries(
        self, *, workspace_id: str, app_id: str, exclude_ids: Collection[str] = ()
    ) -> list[WorkloadSummary]:
        row_identity = func.concat(
            DeploymentTable.id, ":", DeploymentTable.updated_at, ":", DeploymentTable.active
        )
        statement = (
            select(
                DeploymentTable.kind,
                DeploymentTable.name,
                func.count(),
                func.md5(
                    func.string_agg(
                        row_identity, aggregate_order_by(literal(","), DeploymentTable.id)
                    )
                ),
            )
            .where(
                DeploymentTable.workspace_id == workspace_id,
                DeploymentTable.app_id == app_id,
                DeploymentTable.deleted_at.is_(None),
                DeploymentTable.id.not_in(exclude_ids),
            )
            .group_by(DeploymentTable.kind, DeploymentTable.name)
            .order_by(DeploymentTable.kind, DeploymentTable.name)
        )
        return [
            WorkloadSummary(DeploymentKind(kind), name, count, fingerprint)
            for kind, name, count, fingerprint in self.session.execute(statement).tuples()
        ]

    def submitted_workloads(
        self, *, workspace_id: str, app_id: str, deployment_ids: Collection[str]
    ) -> list[tuple[DeploymentKind, str]]:
        return [
            (DeploymentKind(kind), name)
            for kind, name in self.session.execute(
                select(DeploymentTable.kind, DeploymentTable.name).where(
                    DeploymentTable.workspace_id == workspace_id,
                    DeploymentTable.app_id == app_id,
                    DeploymentTable.id.in_(deployment_ids),
                    DeploymentTable.deleted_at.is_(None),
                    DeploymentTable.stub_id.is_not(None),
                )
            ).tuples()
        ]

    def retire(
        self,
        *,
        workspace_id: str,
        app_id: str,
        workloads: Collection[tuple[DeploymentKind, str]],
        now: datetime,
    ) -> list[tuple[str, str]]:
        return list(
            self.session.execute(
                update(DeploymentTable)
                .where(
                    DeploymentTable.workspace_id == workspace_id,
                    DeploymentTable.app_id == app_id,
                    DeploymentTable.deleted_at.is_(None),
                    tuple_(DeploymentTable.kind, DeploymentTable.name).in_(
                        [(kind.value, name) for kind, name in workloads]
                    ),
                )
                .values(active=False, deleted_at=now, updated_at=now)
                .returning(DeploymentTable.id, DeploymentTable.name)
            ).tuples()
        )

    def targets(self, operation_id: str) -> list[str]:
        return list(
            self.session.scalars(
                select(DeploymentPruneTargetTable.deployment_id)
                .where(DeploymentPruneTargetTable.operation_id == operation_id)
                .order_by(DeploymentPruneTargetTable.deployment_id)
            )
        )

    def create_operation(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        app_id: str,
        request_digest: str,
        deployment_ids: Collection[str],
        now: datetime,
    ) -> PruneOperation:
        row = DeploymentPruneTable(
            id=operation_id,
            workspace_id=workspace_id,
            app_id=app_id,
            request_digest=request_digest,
            retry_at=now,
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        self.session.flush()
        self.session.add_all(
            [
                DeploymentPruneTargetTable(operation_id=operation_id, deployment_id=deployment_id)
                for deployment_id in deployment_ids
            ]
        )
        self.session.flush()
        return _operation(row)

    def operation(self, operation_id: str, *, workspace_id: str) -> PruneOperation | None:
        row = self.session.scalar(
            select(DeploymentPruneTable).where(
                DeploymentPruneTable.id == operation_id,
                DeploymentPruneTable.workspace_id == workspace_id,
            )
        )
        return _operation(row) if row is not None else None

    def due(self, *, now: datetime, retry_at: datetime, limit: int) -> list[PruneOperation]:
        rows = list(
            self.session.scalars(
                select(DeploymentPruneTable)
                .where(
                    DeploymentPruneTable.completed_at.is_(None),
                    DeploymentPruneTable.retry_at <= now,
                )
                .order_by(DeploymentPruneTable.retry_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.retry_at = retry_at
        return [_operation(row) for row in rows]

    def complete(self, operation_id: str, *, workspace_id: str, now: datetime) -> None:
        self.session.execute(
            update(DeploymentPruneTable)
            .where(
                DeploymentPruneTable.id == operation_id,
                DeploymentPruneTable.workspace_id == workspace_id,
                DeploymentPruneTable.completed_at.is_(None),
            )
            .values(completed_at=now, updated_at=now)
        )

    def container_targets(
        self, *, workspace_id: str, deployment_ids: Collection[str]
    ) -> list[ContainerShutdownTarget]:
        stubs = select(StubTable.id).where(
            StubTable.workspace_id == workspace_id,
            StubTable.deployment_id.in_(deployment_ids),
        )
        return [
            ContainerShutdownTarget(container_id=container_id, worker_id=worker_id or "")
            for container_id, worker_id in self.session.execute(
                select(
                    ContainerTable.id,
                    func.coalesce(
                        func.nullif(ContainerTable.runtime_worker_id, ""),
                        cast(ContainerTable.worker_id, String),
                    ),
                ).where(
                    ContainerTable.workspace_id == workspace_id,
                    ContainerTable.stub_id.in_(stubs),
                    container_storage_release_pending(),
                )
            ).tuples()
        ]


def _operation(row: DeploymentPruneTable) -> PruneOperation:
    return PruneOperation(
        row.id,
        row.workspace_id,
        row.app_id,
        row.request_digest,
        row.created_at,
        row.completed_at is not None,
    )
