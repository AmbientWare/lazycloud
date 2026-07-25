from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.http.apps import AppResponse
from shared.http.base import HttpModel
from shared.http.compute import ContainerResponse
from shared.http.deployments import DeploymentResponse
from shared.http.stubs import StubResponse
from shared.tasks import TaskStatus


class TaskActionCapabilitiesResponse(HttpModel):
    can_cancel: bool = False
    can_rerun: bool = False
    can_shell: bool = False


class TaskResponse(HttpModel):
    id: str
    name: str
    status: TaskStatus = TaskStatus.Pending
    workspace_id: str | None = None
    app_id: str | None = None
    stub_id: str | None = None
    deployment_id: str | None = None
    container_id: str | None = None
    parent_task_id: str | None = None
    root_task_id: str | None = None
    handler: str | None = None
    command: list[str] = Field(default_factory=list)
    args: list[JsonValue] = Field(default_factory=list)
    kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    attempt_number: int = 0
    max_attempts: int = 1
    next_retry_at: datetime | None = None
    result: JsonValue = None
    error: str | None = None
    exit_code: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    app: AppResponse | None = None
    workload: StubResponse | None = None
    deployment: DeploymentResponse | None = None
    container: ContainerResponse | None = None
    actions: TaskActionCapabilitiesResponse = Field(default_factory=TaskActionCapabilitiesResponse)


class TaskPageResponse(HttpModel):
    data: list[TaskResponse] = Field(default_factory=list)
    next: str = ""


class TaskCountByDeploymentResponse(HttpModel):
    deployment_id: str
    count: int
    status_counts: dict[TaskStatus, int] = Field(default_factory=dict)


class TaskCountByDeploymentListResponse(HttpModel):
    items: list[TaskCountByDeploymentResponse] = Field(default_factory=list)


class TaskTimeWindowBucketResponse(HttpModel):
    timestamp: datetime
    count: int
    status_counts: dict[TaskStatus, int] = Field(default_factory=dict)


class TaskTimeWindowBucketListResponse(HttpModel):
    items: list[TaskTimeWindowBucketResponse] = Field(default_factory=list)


class TaskStopResponse(HttpModel):
    stopped: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class TaskMetricsSummaryResponse(HttpModel):
    total: int
    status_counts: dict[TaskStatus, int] = Field(default_factory=dict)
    completed: int
    failed: int
    cancelled: int
    failure_rate: float = 0.0
    average_runtime_ms: float | None = None
    runtime_ms_p50: float | None = None
    runtime_ms_p95: float | None = None
    runtime_ms_p99: float | None = None
    startup_ms_p50: float | None = None
    startup_ms_p95: float | None = None


class TaskLogEntryResponse(HttpModel):
    id: str
    task_id: str
    stream: str = "system"
    message: str
    created_at: datetime


class TaskLogListResponse(HttpModel):
    logs: list[TaskLogEntryResponse] = Field(default_factory=list)


__all__ = [
    "TaskActionCapabilitiesResponse",
    "TaskCountByDeploymentListResponse",
    "TaskCountByDeploymentResponse",
    "TaskLogEntryResponse",
    "TaskLogListResponse",
    "TaskMetricsSummaryResponse",
    "TaskPageResponse",
    "TaskResponse",
    "TaskStopResponse",
    "TaskTimeWindowBucketListResponse",
    "TaskTimeWindowBucketResponse",
]
