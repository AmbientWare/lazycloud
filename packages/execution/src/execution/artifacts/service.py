from __future__ import annotations

import base64
from datetime import datetime, timedelta
from urllib.parse import quote
from uuid import UUID, uuid4

from billing.retention import workspace_retention_days
from database.repositories.apps import AppRepository
from database.repositories.artifacts import ArtifactRepository
from database.repositories.billing_rates import PlatformRateRepository
from database.repositories.execution import TaskRepository
from pydantic import ValidationError
from shared.artifacts import ArtifactObjectFields
from shared.billing_quotes import LedgerComponent
from shared.billing_rate_card import SECONDS_PER_30_DAY_MONTH
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.artifacts import (
    ArtifactListResponse,
    ArtifactSaveResponse,
    ArtifactStorageSummary,
    ArtifactSummary,
)
from shared.http.base import HttpModel
from shared.objects import ObjectRecord
from shared.timestamps import to_utc, utc_now
from storage.service import ObjectStorage

from execution.artifacts.planning import (
    DEFAULT_ARTIFACT_PUBLIC_URL_EXPIRES_SECONDS,
    ArtifactPublicUrlPlan,
    ArtifactStatPlan,
    plan_artifact_path,
    plan_artifact_public_url,
)
from execution.context import ExecutionContext


class ArtifactCursor(HttpModel):
    created_at: datetime
    id: UUID


def artifact_summary(record: ObjectRecord) -> ArtifactSummary:
    if record.artifact_expires_at is None:
        raise RuntimeError("stored task artifact has no expiration")
    return ArtifactSummary(
        id=record.id,
        task_id=record.artifact_task_id or "",
        filename=record.artifact_filename,
        content_type=record.content_type,
        size=record.size,
        created_at=record.created_at,
        app_id=record.artifact_app_id,
        app_name=record.artifact_app_name,
        expires_at=record.artifact_expires_at,
        deleting=record.cleanup_claimed_at is not None,
        deletion_failed=record.artifact_deletion_failed,
    )


class ArtifactStorageService:
    def __init__(
        self,
        context: ExecutionContext,
        *,
        object_storage: ObjectStorage,
    ) -> None:
        self.context = context
        self.object_storage = object_storage
        self.bucket = object_storage.default_bucket

    def save(
        self,
        *,
        workspace_id: str,
        task_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> ArtifactSaveResponse:
        with self.context.database.session() as session:
            task = TaskRepository(session).get(task_id, workspace_id=workspace_id)
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            app = (
                AppRepository(session).get(
                    task.app_id, workspace_id=workspace_id, include_deleted=True
                )
                if task.app_id
                else None
            )
            seconds = workspace_retention_days(session, workspace_id) * 24 * 60 * 60
        identifier = str(uuid4())
        stub_id = task.deployment_id or "standalone"
        path = plan_artifact_path(workspace_id, stub_id, task.id, identifier, filename)
        record = self.object_storage.put_bytes_for_workspace(
            workspace_id=workspace_id,
            bucket=self.bucket,
            key=path.storage_key,
            data=content,
            object_id=identifier,
            content_type=content_type,
            metadata={
                "artifact_id": identifier,
                "task_id": task.id,
                "workspace_id": workspace_id,
                "filename": quote(path.filename, safe=""),
                "stub_external_id": stub_id,
            },
            artifact=ArtifactObjectFields(
                artifact_task_id=task.id,
                artifact_app_id=task.app_id,
                artifact_app_name=app.name if app else "",
                artifact_filename=path.filename,
                artifact_retention_seconds=seconds,
            ),
        )
        if record.artifact_expires_at is None:
            raise RuntimeError("stored task artifact has no expiration")
        return ArtifactSaveResponse(
            id=record.id,
            expires_at=record.artifact_expires_at,
        )

    def list(
        self,
        *,
        workspace_id: str,
        task_id: str | None = None,
        app_id: str | None = None,
        search: str = "",
        content_type: str = "",
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        cursor: str = "",
        limit: int = 50,
    ) -> ArtifactListResponse:
        position: tuple[datetime, str] | None = None
        if cursor:
            try:
                parsed = ArtifactCursor.model_validate_json(base64.urlsafe_b64decode(cursor))
                position = (parsed.created_at, str(parsed.id))
            except (ValueError, ValidationError) as exc:
                raise InvalidInputError("invalid artifact cursor") from exc
        with self.context.database.session() as session:
            records = ArtifactRepository(session).page(
                workspace_id=workspace_id,
                task_id=task_id,
                app_id=app_id,
                search=search,
                content_type=content_type,
                created_after=created_after,
                created_before=created_before,
                cursor=position,
                limit=limit + 1,
            )
        next_cursor = ""
        if len(records) > limit:
            last = records[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                ArtifactCursor(created_at=last.created_at, id=UUID(last.id))
                .model_dump_json()
                .encode()
            ).decode()
        return ArtifactListResponse(
            data=[artifact_summary(record) for record in records[:limit]], next=next_cursor
        )

    def summary(self, *, workspace_id: str) -> ArtifactStorageSummary:
        now = utc_now()
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with self.context.database.session() as session:
            repository = ArtifactRepository(session)
            count, size = repository.totals(workspace_id)
            quotes = PlatformRateRepository(session).quotes_for(
                component=LedgerComponent.VolumeStorage,
                started_at=now,
                ended_at=now + timedelta(microseconds=1),
            )
            rate = next(
                (
                    quote.rate_nanos_per_unit
                    for quote in quotes
                    if quote.effective_at <= now
                    and (quote.valid_until is None or quote.valid_until > now)
                ),
                None,
            )
            return ArtifactStorageSummary(
                count=count,
                size_bytes=size,
                estimated_monthly_nanos=int(rate * size * SECONDS_PER_30_DAY_MONTH)
                if rate is not None
                else None,
                accrued_nanos=repository.accrued_cost(workspace_id, since=since),
                accrued_since=since,
                retention_seconds=workspace_retention_days(session, workspace_id) * 24 * 60 * 60,
            )

    def delete(self, *, workspace_id: str, artifact_id: str) -> None:
        with self.context.database.session() as session:
            record = ArtifactRepository(session).get(artifact_id, workspace_id=workspace_id)
        if record is not None:
            self.object_storage.delete_for_workspace(
                workspace_id=workspace_id, bucket=record.bucket, key=record.key
            )

    def stat(
        self, *, workspace_id: str, task_id: str, artifact_id: str, filename: str
    ) -> ArtifactStatPlan:
        record = self._record(
            workspace_id=workspace_id, artifact_id=artifact_id, task_id=task_id, filename=filename
        )
        return ArtifactStatPlan(
            mode="0644",
            artifact_id=artifact_id,
            task_id=task_id,
            filename=filename,
            size=record.size,
            accessed_at=record.updated_at,
            modified_at=record.artifact_stored_at or record.created_at,
        )

    def read_content(
        self, *, workspace_id: str, task_id: str, artifact_id: str, filename: str
    ) -> tuple[bytes, str, str]:
        record = self._record(
            workspace_id=workspace_id, artifact_id=artifact_id, task_id=task_id, filename=filename
        )
        content = self.object_storage.read_bytes_for_workspace(
            workspace_id=workspace_id, bucket=record.bucket, key=record.key
        )
        return content, record.content_type, record.artifact_filename

    def public_url(
        self,
        *,
        workspace_id: str,
        task_id: str,
        artifact_id: str,
        filename: str,
        expires_seconds: int = DEFAULT_ARTIFACT_PUBLIC_URL_EXPIRES_SECONDS,
    ) -> ArtifactPublicUrlPlan:
        record = self._record(
            workspace_id=workspace_id, artifact_id=artifact_id, task_id=task_id, filename=filename
        )
        if record.artifact_expires_at is not None:
            expires_seconds = min(
                expires_seconds,
                max(0, int((to_utc(record.artifact_expires_at) - utc_now()).total_seconds())),
            )
        url = self.object_storage.generate_presigned_get_url_for_workspace(
            workspace_id=workspace_id,
            bucket=record.bucket,
            key=record.key,
            expires_seconds=expires_seconds,
        )
        return plan_artifact_public_url(
            artifact_id=artifact_id,
            target_path=record.key,
            expires_seconds=expires_seconds,
            presigned_url=url,
        )

    def _record(
        self, *, workspace_id: str, artifact_id: str, task_id: str, filename: str
    ) -> ObjectRecord:
        with self.context.database.session() as session:
            record = ArtifactRepository(session).get(artifact_id, workspace_id=workspace_id)
        if (
            record is None
            or record.artifact_task_id != task_id
            or record.artifact_filename != filename
        ):
            raise NotFoundError(f"artifact not found: {artifact_id}")
        self._assert_available(record)
        return record

    @staticmethod
    def _assert_available(record: ObjectRecord) -> None:
        if record.write_claimed_at is not None or record.cleanup_claimed_at is not None:
            raise ConflictError("artifact storage operation is in progress")
        if (
            record.artifact_expires_at is not None
            and to_utc(record.artifact_expires_at) <= utc_now()
        ):
            raise NotFoundError("artifact has expired")


__all__ = ["ArtifactStorageService"]
