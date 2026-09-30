from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from database.repositories.deployment_plans import DeploymentPlanRepository
from database.repositories.execution import EventRepository
from database.tables.apps import AppDeploymentIntentTable
from database.tables.deployment_effects import (
    DeploymentEffectTable,
    DeploymentPreparationTable,
    DeploymentShutdownTable,
)
from pydantic import JsonValue
from shared.container_requests import ContainerShutdownTarget
from shared.deployment_records import Deployment
from shared.events import Event
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session


class DeploymentAction(StrEnum):
    Created = "created"
    Started = "started"
    Stopped = "stopped"
    Deleted = "deleted"
    Scaled = "scale.updated"


@dataclass(frozen=True, slots=True)
class DeploymentEffect:
    id: str
    workspace_id: str
    deployment_id: str
    action: DeploymentAction
    created_at: datetime
    app_created: bool
    source_stub_id: str | None
    source_deleted: bool
    app_revision: int | None


@dataclass(slots=True)
class DeploymentEffectRepository:
    session: Session

    def prepare(self, deployment: Deployment, *, workspace_id: str, now: datetime) -> None:
        self.session.add(
            DeploymentPreparationTable(
                id=deployment.id,
                workspace_id=workspace_id,
                deployment=deployment.model_dump(mode="json"),
                expires_at=now + timedelta(minutes=10),
            )
        )

    def finish_preparation(self, deployment_id: str, *, now: datetime) -> bool:
        return (
            self.session.scalar(
                delete(DeploymentPreparationTable)
                .where(
                    DeploymentPreparationTable.id == deployment_id,
                    DeploymentPreparationTable.expires_at > now,
                )
                .returning(DeploymentPreparationTable.id)
            )
            is not None
        )

    def preparations(self, workspace_ids: Collection[str], *, now: datetime) -> list[Deployment]:
        return [
            Deployment.model_validate(payload)
            for payload in self.session.scalars(
                select(DeploymentPreparationTable.deployment).where(
                    DeploymentPreparationTable.workspace_id.in_(workspace_ids),
                    DeploymentPreparationTable.expires_at > now,
                )
            )
        ]

    def expired_preparations(self, *, now: datetime, limit: int) -> list[tuple[str, str]]:
        return list(
            self.session.execute(
                select(DeploymentPreparationTable.id, DeploymentPreparationTable.workspace_id)
                .where(
                    DeploymentPreparationTable.expires_at <= now,
                )
                .order_by(DeploymentPreparationTable.expires_at)
                .limit(limit)
            ).tuples()
        )

    def delete_preparations(self, preparation_ids: list[str]) -> None:
        self.session.execute(
            delete(DeploymentPreparationTable).where(
                DeploymentPreparationTable.id.in_(preparation_ids)
            )
        )

    def record(
        self,
        deployment: Deployment,
        *,
        workspace_id: str,
        action: DeploymentAction,
        app_created: bool = False,
        source_stub_id: str | None = None,
        source_deleted: bool = False,
        app_revision: int | None = None,
        event_id: str | None = None,
        created_at: datetime | None = None,
        data: dict[str, JsonValue] | None = None,
        message: str | None = None,
    ) -> DeploymentEffect:
        if event_id is not None:
            pending = self.session.get(DeploymentEffectTable, event_id)
            if pending is not None:
                return _effect(pending)
        occurred_at = created_at or deployment.updated_at
        row = DeploymentEffectTable(
            id=event_id or str(uuid4()),
            workspace_id=workspace_id,
            deployment_id=deployment.id,
            action=action.value,
            app_created=app_created,
            source_stub_id=source_stub_id,
            source_deleted=source_deleted,
            app_revision=app_revision,
            created_at=occurred_at,
            updated_at=occurred_at,
            retry_at=occurred_at + timedelta(seconds=60),
        )
        self.session.add(row)
        self.session.flush()
        if app_revision is None and action in {DeploymentAction.Stopped, DeploymentAction.Deleted}:
            targets = DeploymentPlanRepository(self.session).container_targets(
                workspace_id=workspace_id, deployment_ids=[deployment.id]
            )
            self.session.add_all(
                [
                    DeploymentShutdownTable(
                        effect_id=row.id,
                        container_id=target.container_id,
                        worker_id=target.worker_id,
                    )
                    for target in targets
                ]
            )
        EventRepository(self.session).append(
            Event(
                id=row.id,
                action=f"deployment.{action.value}",
                resource_type="deployment",
                resource_id=deployment.id,
                message=message or f"{action.value} deployment {deployment.name}",
                data=data or {},
                created_at=occurred_at,
            ),
            workspace_id=workspace_id,
        )
        return _effect(row)

    def due(self, *, now: datetime, limit: int) -> list[DeploymentEffect]:
        rows = list(
            self.session.scalars(
                select(DeploymentEffectTable)
                .where(DeploymentEffectTable.retry_at <= now)
                .order_by(DeploymentEffectTable.retry_at, DeploymentEffectTable.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.retry_at = now + timedelta(seconds=60)
        return [_effect(row) for row in rows]

    def targets(self, effect_id: str) -> list[ContainerShutdownTarget]:
        return [
            ContainerShutdownTarget(container_id=container_id, worker_id=worker_id)
            for container_id, worker_id in self.session.execute(
                select(
                    DeploymentShutdownTable.container_id, DeploymentShutdownTable.worker_id
                ).where(DeploymentShutdownTable.effect_id == effect_id)
            )
        ]

    def complete(self, effect: DeploymentEffect, *, now: datetime) -> None:
        if effect.app_revision is not None:
            self.session.execute(
                update(AppDeploymentIntentTable)
                .where(
                    AppDeploymentIntentTable.event_id == effect.id,
                    AppDeploymentIntentTable.operation_revision == effect.app_revision,
                )
                .values(workspace_change_published_at=now, updated_at=now)
            )
        self.session.execute(
            delete(DeploymentEffectTable).where(DeploymentEffectTable.id == effect.id)
        )


def _effect(row: DeploymentEffectTable) -> DeploymentEffect:
    return DeploymentEffect(
        row.id,
        row.workspace_id,
        row.deployment_id,
        DeploymentAction(row.action),
        row.created_at,
        row.app_created,
        row.source_stub_id,
        row.source_deleted,
        row.app_revision,
    )
