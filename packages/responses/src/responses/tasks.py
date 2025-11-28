from uuid import UUID

from models.statuses import TaskStatus
from pydantic import BaseModel


class TaskStatusResponse(BaseModel):
    task_id: UUID
    status: TaskStatus
    message: str | None = None


class DeploymentTaskStatusResponse(TaskStatusResponse):
    deployment_id: str


class InstanceTaskStatusResponse(TaskStatusResponse):
    deployment_id: str
    service_name: str
    pod_name: str


class ServiceTaskStatusResponse(TaskStatusResponse):
    deployment_id: str
