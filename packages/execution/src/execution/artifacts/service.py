from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, unquote
from uuid import uuid4

from database.repositories.execution import TaskRepository
from shared.bytes_transport import encode_bytes
from shared.errors import InvalidInputError, NotFoundError
from shared.http.artifacts import ArtifactPreviewResponse
from shared.objects import ObjectRecord
from shared.tasks import Task
from storage.service import ObjectStorage
from storage_client.s3 import S3ObjectStoreSettings

from execution.artifacts.planning import (
    DEFAULT_ARTIFACT_PUBLIC_URL_EXPIRES_SECONDS,
    ArtifactPathPlan,
    ArtifactPublicUrlPlan,
    ArtifactStatPlan,
    artifact_storage_prefix,
    plan_artifact_path,
    plan_artifact_public_url,
)
from execution.context import ExecutionContext

ARTIFACT_METADATA_ID = "artifact_id"
ARTIFACT_METADATA_TASK_ID = "task_id"
ARTIFACT_METADATA_WORKSPACE_ID = "workspace_id"
ARTIFACT_METADATA_FILENAME = "filename"
ARTIFACT_METADATA_STUB_ID = "stub_external_id"


def _encode_metadata_filename(filename: str) -> str:
    """Percent-encode a filename for object metadata.

    Object metadata is US-ASCII by protocol, and the store rejects the whole
    upload when it is not. A name like ``résumé.txt`` is ordinary, so it is
    encoded on the way in rather than costing the user their artifact.
    """
    return quote(filename, safe="")


def _decode_metadata_filename(value: str) -> str:
    return unquote(value)


@dataclass(frozen=True, slots=True)
class ArtifactListing:
    artifact_id: str
    task_id: str
    filename: str
    content_type: str
    size: int
    created_at: datetime


class ArtifactStorageService:
    def __init__(
        self,
        context: ExecutionContext,
        *,
        object_storage: ObjectStorage | None = None,
        bucket: str | None = None,
    ) -> None:
        self.context = context
        self.object_storage = object_storage or ObjectStorage(context)
        self.bucket = bucket or S3ObjectStoreSettings().bucket

    def save(
        self,
        *,
        workspace_id: str,
        task_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        task = self._task(task_id, workspace_id=workspace_id)
        artifact_id = str(uuid4())
        stub_external_id = self._stub_external_id(task)
        path = plan_artifact_path(
            workspace_id,
            stub_external_id,
            task.id,
            artifact_id,
            filename,
        )
        self.object_storage.put_bytes_for_workspace(
            workspace_id=workspace_id,
            bucket=self.bucket,
            key=path.storage_key,
            data=content,
            object_id=artifact_id,
            content_type=content_type,
            metadata={
                ARTIFACT_METADATA_ID: artifact_id,
                ARTIFACT_METADATA_TASK_ID: task.id,
                ARTIFACT_METADATA_WORKSPACE_ID: workspace_id,
                ARTIFACT_METADATA_FILENAME: _encode_metadata_filename(path.filename),
                ARTIFACT_METADATA_STUB_ID: stub_external_id,
            },
        )
        return artifact_id

    def list_for_task(
        self,
        *,
        workspace_id: str,
        task_id: str,
    ) -> list[ArtifactListing]:
        """Every artifact a task saved, newest first.

        Artifacts are records in object storage rather than rows of their own,
        so the task's own prefix is what scopes the listing.
        """
        task = self._task(task_id, workspace_id=workspace_id)
        prefix = artifact_storage_prefix(self._stub_external_id(task), task.id)
        records = self.object_storage.list_for_workspace(
            workspace_id=workspace_id,
            bucket=self.bucket,
            prefix=prefix,
        )
        listings = [
            ArtifactListing(
                artifact_id=record.metadata.get(ARTIFACT_METADATA_ID, record.id),
                task_id=task.id,
                filename=_decode_metadata_filename(
                    record.metadata.get(ARTIFACT_METADATA_FILENAME, "")
                )
                or record.key.rsplit("/", 1)[-1],
                content_type=record.content_type or "application/octet-stream",
                size=record.size or 0,
                created_at=record.created_at,
            )
            for record in records
        ]
        listings.sort(key=lambda item: item.created_at, reverse=True)
        return listings

    def stat(
        self,
        *,
        workspace_id: str,
        task_id: str,
        artifact_id: str,
        filename: str,
    ) -> ArtifactStatPlan:
        path, record = self._path_and_record(
            workspace_id=workspace_id,
            task_id=task_id,
            artifact_id=artifact_id,
            filename=filename,
        )
        object_info = self.object_storage.head_for_workspace(
            workspace_id=workspace_id,
            bucket=self.bucket,
            key=path.storage_key,
        )
        modified_at = object_info.last_modified or record.updated_at
        return ArtifactStatPlan(
            artifact_id=artifact_id,
            task_id=task_id,
            filename=path.filename,
            mode="0644",
            size=object_info.size if object_info.size is not None else record.size,
            accessed_at=modified_at,
            modified_at=modified_at,
        )

    def read_content(
        self,
        *,
        workspace_id: str,
        task_id: str,
        artifact_id: str,
        filename: str,
    ) -> tuple[bytes, str, str]:
        """Return an artifact's bytes, content type, and filename.

        Serving content through the control plane keeps a reader on one origin.
        A presigned URL names the object store directly, which a browser outside
        the deployment's network often cannot reach.
        """
        path, record = self._path_and_record(
            workspace_id=workspace_id,
            task_id=task_id,
            artifact_id=artifact_id,
            filename=filename,
        )
        content = self.object_storage.read_bytes_for_workspace(
            workspace_id=workspace_id,
            bucket=self.bucket,
            key=path.storage_key,
        )
        return (content, record.content_type or "application/octet-stream", path.filename)

    def public_url(
        self,
        *,
        workspace_id: str,
        task_id: str,
        artifact_id: str,
        filename: str,
        expires_seconds: int = DEFAULT_ARTIFACT_PUBLIC_URL_EXPIRES_SECONDS,
    ) -> ArtifactPublicUrlPlan:
        path, _ = self._path_and_record(
            workspace_id=workspace_id,
            task_id=task_id,
            artifact_id=artifact_id,
            filename=filename,
        )
        presigned_url = self.object_storage.generate_presigned_get_url_for_workspace(
            workspace_id=workspace_id,
            bucket=self.bucket,
            key=path.storage_key,
            expires_seconds=expires_seconds,
        )
        return plan_artifact_public_url(
            artifact_id=artifact_id,
            target_path=path.storage_key,
            expires_seconds=expires_seconds,
            presigned_url=presigned_url,
        )

    def preview(
        self,
        *,
        workspace_id: str,
        task_id: str,
        artifact_id: str,
        filename: str,
    ) -> ArtifactPreviewResponse:
        path, record = self._path_and_record(
            workspace_id=workspace_id,
            task_id=task_id,
            artifact_id=artifact_id,
            filename=filename,
        )
        content_type = record.content_type or "application/octet-stream"
        binary_preview = content_type.startswith("image/") or content_type == "application/pdf"
        limit = 8 * 1024 * 1024 if binary_preview else 256 * 1024
        content = (
            b""
            if record.size == 0
            else self.object_storage.read_bytes_for_workspace(
                workspace_id=workspace_id,
                bucket=self.bucket,
                key=path.storage_key,
                max_bytes=limit + 1,
            )
        )
        truncated = len(content) > limit
        if binary_preview and truncated:
            raise InvalidInputError(
                "This file exceeds the 8 MiB preview limit. Download it to inspect it."
            )
        return ArtifactPreviewResponse(
            value_base64=encode_bytes(content[:limit]),
            content_type=content_type,
            truncated=truncated,
        )

    def _path_and_record(
        self,
        *,
        workspace_id: str,
        task_id: str,
        artifact_id: str,
        filename: str,
    ) -> tuple[ArtifactPathPlan, ObjectRecord]:
        task = self._task(task_id, workspace_id=workspace_id)
        path = plan_artifact_path(
            workspace_id,
            self._stub_external_id(task),
            task.id,
            artifact_id,
            filename,
        )
        return path, self.object_storage.get_for_workspace(
            workspace_id=workspace_id,
            bucket=self.bucket,
            key=path.storage_key,
        )

    def _task(self, task_id: str, *, workspace_id: str) -> Task:
        with self.context.database.session() as session:
            task = TaskRepository(session).get(task_id, workspace_id=workspace_id)
        if task is None:
            msg = f"task not found: {task_id}"
            raise NotFoundError(msg)
        return task

    def _stub_external_id(self, task: Task) -> str:
        return task.deployment_id or "standalone"


__all__ = ["ArtifactStorageService"]
