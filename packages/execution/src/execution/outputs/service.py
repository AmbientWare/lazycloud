from __future__ import annotations

from uuid import uuid4

from database.repositories.execution import TaskRepository
from shared.errors import NotFoundError
from shared.objects import ObjectRecord
from shared.tasks import Task
from storage.service import ObjectStorage
from storage_client.s3 import S3ObjectStoreSettings

from execution.context import ExecutionContext
from execution.outputs.planning import (
    DEFAULT_OUTPUT_PUBLIC_URL_EXPIRES_SECONDS,
    OutputPathPlan,
    OutputPublicUrlPlan,
    OutputStatPlan,
    OutputStorageMode,
    plan_output_path,
    plan_output_public_url,
)

OUTPUT_METADATA_ID = "output_id"
OUTPUT_METADATA_TASK_ID = "task_id"
OUTPUT_METADATA_WORKSPACE_ID = "workspace_id"
OUTPUT_METADATA_FILENAME = "filename"
OUTPUT_METADATA_STUB_ID = "stub_external_id"


class OutputStorageService:
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
        output_id = str(uuid4())
        stub_external_id = self._stub_external_id(task)
        path = plan_output_path(
            workspace_id,
            stub_external_id,
            task.id,
            output_id,
            filename,
        )
        self.object_storage.put_bytes_for_workspace(
            workspace_id=workspace_id,
            bucket=self.bucket,
            key=path.storage_key,
            data=content,
            object_id=output_id,
            content_type=content_type,
            metadata={
                OUTPUT_METADATA_ID: output_id,
                OUTPUT_METADATA_TASK_ID: task.id,
                OUTPUT_METADATA_WORKSPACE_ID: workspace_id,
                OUTPUT_METADATA_FILENAME: path.filename,
                OUTPUT_METADATA_STUB_ID: stub_external_id,
            },
        )
        return output_id

    def stat(
        self,
        *,
        workspace_id: str,
        task_id: str,
        output_id: str,
        filename: str,
    ) -> OutputStatPlan:
        path, record = self._path_and_record(
            workspace_id=workspace_id,
            task_id=task_id,
            output_id=output_id,
            filename=filename,
        )
        object_info = self.object_storage.head_for_workspace(
            workspace_id=workspace_id,
            bucket=self.bucket,
            key=path.storage_key,
        )
        modified_at = object_info.last_modified or record.updated_at
        return OutputStatPlan(
            output_id=output_id,
            task_id=task_id,
            filename=path.filename,
            mode="0644",
            size=object_info.size if object_info.size is not None else record.size,
            accessed_at=modified_at,
            modified_at=modified_at,
        )

    def public_url(
        self,
        *,
        workspace_id: str,
        task_id: str,
        output_id: str,
        filename: str,
        gateway_external_url: str,
        expires_seconds: int = DEFAULT_OUTPUT_PUBLIC_URL_EXPIRES_SECONDS,
    ) -> OutputPublicUrlPlan:
        path, _ = self._path_and_record(
            workspace_id=workspace_id,
            task_id=task_id,
            output_id=output_id,
            filename=filename,
        )
        presigned_url = self.object_storage.generate_presigned_get_url_for_workspace(
            workspace_id=workspace_id,
            bucket=self.bucket,
            key=path.storage_key,
            expires_seconds=expires_seconds,
        )
        return plan_output_public_url(
            output_id=output_id,
            target_path=path.storage_key,
            gateway_external_url=gateway_external_url,
            expires_seconds=expires_seconds,
            storage_mode=OutputStorageMode.WorkspaceObjectStorage,
            presigned_url=presigned_url,
        )

    def _path_and_record(
        self,
        *,
        workspace_id: str,
        task_id: str,
        output_id: str,
        filename: str,
    ) -> tuple[OutputPathPlan, ObjectRecord]:
        task = self._task(task_id, workspace_id=workspace_id)
        path = plan_output_path(
            workspace_id,
            self._stub_external_id(task),
            task.id,
            output_id,
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


__all__ = ["OutputStorageService"]
