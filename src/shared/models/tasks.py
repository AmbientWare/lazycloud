from uuid import UUID

from pydantic import BaseModel

from shared.models.statuses import TaskStatus


class TaskStatusResponse(BaseModel):
    task_id: UUID
    status: TaskStatus
    message: str | None = None


class DeploymentTaskStatusResponse(TaskStatusResponse):
    deployment_id: str
