from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator, model_validator

from shared.enums import StringEnum
from shared.events import Event
from shared.http.base import HttpModel
from shared.logs import LogEntry


class LogObjectType(StringEnum):
    Deployment = "deployment"
    Task = "task"
    Stub = "stub"
    Container = "container"
    Workspace = "workspace"
    App = "app"
    Machine = "machine"


class LogRecord(HttpModel):
    id: str = ""
    cursor: str = ""
    seq_num: int = 0
    stored_at_ns: int = 0
    timestamp: datetime
    message: str
    stream: str = ""
    container_id: str = ""
    stub_id: str = ""
    stub_type: str = ""
    task_id: str = ""
    workspace_id: str = ""
    app_id: str = ""
    machine_id: str = ""
    worker_id: str = ""
    pid: int = 0
    process_args: tuple[str, ...] = ()
    process_cwd: str = ""
    process_seq: int = 0

    @classmethod
    def from_entry(cls, entry: LogEntry, *, workspace_id: str = "") -> LogRecord:
        return cls(
            id=entry.id,
            timestamp=entry.created_at,
            message=entry.message,
            stream=entry.stream,
            task_id=entry.task_id,
            workspace_id=workspace_id,
        )


class LogQueryRequest(HttpModel):
    workspace_id: str = "default"
    object_id: str | None = None
    object_type: LogObjectType | None = None
    stub_id: str | None = None
    app_id: str | None = None
    task_id: str | None = None
    container_id: str | None = None
    machine_id: str | None = None
    worker_id: str | None = None
    query: str | None = None
    limit: int = Field(default=100, gt=0)
    page: int = Field(default=0, ge=0)
    start_time: datetime | None = None
    end_time: datetime | None = None
    cursor: str | None = None
    seq_num: int | None = Field(default=None, ge=0)
    wait: int | None = Field(default=None, ge=0)
    clamp: bool | None = None

    @field_validator(
        "object_id",
        "stub_id",
        "app_id",
        "task_id",
        "container_id",
        "machine_id",
        "worker_id",
        "query",
        "cursor",
        mode="before",
    )
    @classmethod
    def normalize_blank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None

    @field_validator("object_type", mode="before")
    @classmethod
    def normalize_object_type(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower().replace("-", "_")
            return normalized.replace("_", "")
        return value

    @model_validator(mode="after")
    def apply_object_filter(self) -> LogQueryRequest:
        if self.object_id and self.object_type is not None:
            match self.object_type:
                case LogObjectType.Task:
                    object.__setattr__(self, "task_id", self.task_id or self.object_id)
                case LogObjectType.Stub:
                    object.__setattr__(self, "stub_id", self.stub_id or self.object_id)
                case LogObjectType.Container:
                    object.__setattr__(
                        self,
                        "container_id",
                        self.container_id or self.object_id,
                    )
                case LogObjectType.App:
                    object.__setattr__(self, "app_id", self.app_id or self.object_id)
                case LogObjectType.Machine:
                    object.__setattr__(self, "machine_id", self.machine_id or self.object_id)
                case LogObjectType.Workspace:
                    object.__setattr__(self, "workspace_id", self.object_id)
        if not self.object_id:
            object.__setattr__(
                self,
                "object_id",
                (
                    self.container_id
                    or self.task_id
                    or self.stub_id
                    or self.app_id
                    or self.machine_id
                    or self.worker_id
                    or self.workspace_id
                ),
            )
        return self


class LogQueryResponse(HttpModel):
    object_id: str = ""
    object_type: LogObjectType | None = None
    data: tuple[LogRecord, ...] = ()
    next: str = ""
    count: int = 0
    total_expected: int = 0
    streams: tuple[str, ...] = ()


class EventHistoryRequest(HttpModel):
    workspace_id: str = "default"
    resource_type: str | None = None
    resource_id: str | None = None
    task_id: str | None = None
    container_id: str | None = None
    limit: int = Field(default=100, gt=0)
    cursor: str | None = None

    @field_validator("cursor")
    @classmethod
    def normalize_cursor(cls, value: str | None) -> str | None:
        return value or None


class EventQueryResponse(HttpModel):
    data: tuple[Event, ...] = ()
    next: str = ""
    count: int = 0


class EventListResponse(HttpModel):
    events: list[Event] = Field(default_factory=list)


class ContainerMetricsPointResponse(HttpModel):
    timestamp: datetime
    sample_interval_ms: int = 0
    cpu_millicores: int = 0
    cpu_total_millicores: int = 0
    cpu_pct: float = 0.0
    memory_rss_bytes: int = 0
    memory_total_bytes: int = 0
    network_recv_bytes: int = 0
    network_sent_bytes: int = 0
    disk_read_bytes: int = 0
    disk_write_bytes: int = 0
    gpu_memory_used_bytes: int = 0
    gpu_memory_total_bytes: int = 0
    gpu_type: str = ""


class ContainerMetricsTimeseriesResponse(HttpModel):
    container_id: str
    points: tuple[ContainerMetricsPointResponse, ...] = ()


class WorkspaceActivityMeasure(StringEnum):
    """What one workspace activity series counts, one row per start."""

    Containers = "containers"
    Tasks = "tasks"


class WorkspaceActivitySeriesKind(StringEnum):
    """Which of a workspace's work a series stands for.

    `Unassigned` is work that belongs to no app — a sandbox opened outside one,
    a shell — and is a real share of the workspace rather than a gap. `Other` is
    every app past the requested cap, summed, so a stacked reading still totals
    the window.
    """

    App = "app"
    Unassigned = "unassigned"
    Other = "other"


class WorkspaceContainerCountsResponse(HttpModel):
    """What one workspace is holding right now, by live container status.

    Only the live statuses, because these count what the workspace currently
    occupies rather than what it has ever run.
    """

    workspace_id: str
    pending: int = 0
    running: int = 0


class WorkspaceActivityBucketResponse(HttpModel):
    timestamp: datetime
    count: int = 0


class WorkspaceActivitySeriesResponse(HttpModel):
    """One stack of a workspace activity chart.

    Every series carries the same bucket timestamps over the whole window,
    zeros included, so a reader never has to decide whether a missing interval
    is quiet or unmeasured.
    """

    kind: WorkspaceActivitySeriesKind
    app_id: str = ""
    app_name: str = ""
    total: int = 0
    buckets: tuple[WorkspaceActivityBucketResponse, ...] = ()


class WorkspaceActivityResponse(HttpModel):
    """A workspace's starts over a window, split by the app they belong to.

    `total` is the whole window's count and not the sum of the series shown:
    the two agree only when nothing was folded into `Other`, and the figure a
    reader is given for the window never depends on how many stacks fit.
    """

    workspace_id: str
    measure: WorkspaceActivityMeasure
    window_seconds: int
    start: datetime
    end: datetime
    total: int = 0
    series: tuple[WorkspaceActivitySeriesResponse, ...] = ()


class TaskLatencyBucketResponse(HttpModel):
    timestamp: datetime
    count: int = 0
    p50_ms: float | None = None
    p95_ms: float | None = None
    cold_starts: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)


class TaskLatencyTimeseriesResponse(HttpModel):
    workspace_id: str
    stub_ids: tuple[str, ...] = ()
    deployment_id: str = ""
    window_seconds: int
    buckets: tuple[TaskLatencyBucketResponse, ...] = ()


__all__ = [
    "ContainerMetricsPointResponse",
    "ContainerMetricsTimeseriesResponse",
    "EventHistoryRequest",
    "EventListResponse",
    "EventQueryResponse",
    "LogObjectType",
    "LogQueryRequest",
    "LogQueryResponse",
    "LogRecord",
    "TaskLatencyBucketResponse",
    "TaskLatencyTimeseriesResponse",
    "WorkspaceActivityBucketResponse",
    "WorkspaceActivityMeasure",
    "WorkspaceActivityResponse",
    "WorkspaceActivitySeriesKind",
    "WorkspaceActivitySeriesResponse",
    "WorkspaceContainerCountsResponse",
]
