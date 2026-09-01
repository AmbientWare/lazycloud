from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.deployments import StubKind
from shared.http.base import HttpModel
from shared.http.compute import ContainerResponse
from shared.tasks import TaskStatus


class TaskActionCapabilitiesResponse(HttpModel):
    can_cancel: bool = False
    can_rerun: bool = False
    can_shell: bool = False


class TaskAppReferenceResponse(HttpModel):
    name: str


class TaskWorkloadReferenceResponse(HttpModel):
    name: str
    kind: StubKind


class TaskDeploymentReferenceResponse(HttpModel):
    name: str
    version: int


class TaskResponse(HttpModel):
    """One task row, with its owning resources named rather than embedded.

    A page repeats whatever a row carries, so a hundred rows of one app would
    carry that app's record a hundred times. What a reader wants from the
    resources around a task is a name, a kind, and a version, and their ids stay
    on the row, so addressing the resource itself needs nothing more.
    """

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
    app: TaskAppReferenceResponse | None = None
    workload: TaskWorkloadReferenceResponse | None = None
    deployment: TaskDeploymentReferenceResponse | None = None
    actions: TaskActionCapabilitiesResponse = Field(default_factory=TaskActionCapabilitiesResponse)


class TaskDetailResponse(TaskResponse):
    """A single task read, which additionally carries the container it ran in.

    Absent from the row payload rather than sent empty there: a null container on
    a row would read as "this task had none", which is a different fact from
    "a listing does not resolve containers".
    """

    container: ContainerResponse | None = None


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


__all__ = [
    "TaskActionCapabilitiesResponse",
    "TaskAppReferenceResponse",
    "TaskCountByDeploymentListResponse",
    "TaskCountByDeploymentResponse",
    "TaskDeploymentReferenceResponse",
    "TaskDetailResponse",
    "TaskMetricsSummaryResponse",
    "TaskPageResponse",
    "TaskResponse",
    "TaskStopResponse",
    "TaskTimeWindowBucketListResponse",
    "TaskTimeWindowBucketResponse",
    "TaskWorkloadReferenceResponse",
]
