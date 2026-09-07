from __future__ import annotations

from collections.abc import Mapping
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
    deployment_id: str = ""
    machine_id: str = ""
    worker_id: str = ""
    pid: int = 0
    process_args: tuple[str, ...] = ()
    process_cwd: str = ""
    process_seq: int = 0

    @classmethod
    def from_entry(
        cls,
        entry: LogEntry,
        *,
        cursor: str = "",
        workspace_id: str = "",
        app_id: str = "",
        deployment_id: str = "",
        stub_id: str = "",
        container_id: str = "",
        machine_id: str = "",
        worker_id: str = "",
    ) -> LogRecord:
        return cls(
            id=entry.id,
            cursor=cursor,
            timestamp=entry.created_at,
            message=entry.message,
            stream=entry.stream,
            task_id=entry.task_id,
            workspace_id=workspace_id,
            app_id=app_id,
            deployment_id=deployment_id,
            stub_id=stub_id,
            container_id=container_id,
            machine_id=machine_id,
            worker_id=worker_id,
        )


class LogQueryRequest(HttpModel):
    workspace_id: str
    object_id: str | None = None
    object_type: LogObjectType | None = None
    stub_id: str | None = None
    app_id: str | None = None
    deployment_id: str | None = None
    task_id: str | None = None
    container_id: str | None = None
    machine_id: str | None = None
    worker_id: str | None = None
    query: str | None = None
    limit: int = Field(
        default=100,
        gt=0,
        le=1_000,
        description="Maximum records in a history page or the initial replay of a live stream.",
    )
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
        "deployment_id",
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
                case LogObjectType.Deployment:
                    object.__setattr__(
                        self,
                        "deployment_id",
                        self.deployment_id or self.object_id,
                    )
                case LogObjectType.Machine:
                    object.__setattr__(self, "machine_id", self.machine_id or self.object_id)
        if not self.object_id:
            object.__setattr__(
                self,
                "object_id",
                (
                    self.container_id
                    or self.task_id
                    or self.stub_id
                    or self.deployment_id
                    or self.app_id
                    or self.machine_id
                    or self.worker_id
                    or self.workspace_id
                ),
            )
        return self


class LogQueryResponse(HttpModel):
    data: tuple[LogRecord, ...] = ()
    next: str = ""


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


class AccountActivityMeasure(StringEnum):
    """What one account activity series reads.

    Two questions in one list, distinguished by unit rather than by name. A start
    is an event the account asked for and is counted; a resource is capacity the
    account held over an interval and is read as a level. Nothing sums a start to
    a core, which is why `AccountActivityUnit` travels beside the measure instead
    of a reader keeping its own map of which is which.
    """

    Containers = "containers"
    Tasks = "tasks"
    Cpu = "cpu"
    Memory = "memory"
    Gpu = "gpu"


class AccountActivityUnit(StringEnum):
    """What one activity reading is denominated in.

    `Starts` counts events inside an interval. The rest are capacity held across
    it, averaged over the seconds the interval actually covers, so the interval
    in progress reads at its true level rather than at the fraction of it that
    has elapsed.
    """

    Starts = "starts"
    Cores = "cores"
    Gibibytes = "gibibytes"
    Gpus = "gpus"


ACTIVITY_MEASURE_UNITS: Mapping[AccountActivityMeasure, AccountActivityUnit] = {
    AccountActivityMeasure.Containers: AccountActivityUnit.Starts,
    AccountActivityMeasure.Tasks: AccountActivityUnit.Starts,
    AccountActivityMeasure.Cpu: AccountActivityUnit.Cores,
    AccountActivityMeasure.Memory: AccountActivityUnit.Gibibytes,
    AccountActivityMeasure.Gpu: AccountActivityUnit.Gpus,
}
"""Which unit each measure answers in.

Held here rather than at either end of the wire: the producer states the unit on
every response and the consumer labels an axis with it, and two copies of this
map are two chances for a chart to name a unit the figures are not in.
"""


class AccountActivitySeriesKind(StringEnum):
    """Which of an account's work a series stands for.

    `Unassigned` is work that belongs to no app — a sandbox opened outside one, a
    shell — and is a real share of the account rather than a gap. `Other` is
    every app past the requested cap, summed, so a stacked reading still totals
    the window.
    """

    App = "app"
    Unassigned = "unassigned"
    Other = "other"


class AccountContainerCountsResponse(HttpModel):
    """What this account is holding right now, by live container status.

    Only the live statuses, because these count what the account currently
    occupies rather than what it has ever run. Summed over every workspace the
    caller belongs to, resolved from membership rather than named by the request.
    """

    pending: int = 0
    running: int = 0


class AccountActivityBucketResponse(HttpModel):
    timestamp: datetime
    value: float = 0.0


class AccountActivitySeriesResponse(HttpModel):
    """One stack of an account activity chart.

    Identified by workspace and app together, because an account reads several
    workspaces at once and two of them may hold apps of the same name — merged on
    name alone, one band would carry two customers' worth of work under a label
    naming neither.

    Every series carries the same bucket timestamps over the whole window, zeros
    included, so a reader never has to decide whether a missing interval is quiet
    or unmeasured.
    """

    kind: AccountActivitySeriesKind
    workspace_id: str = ""
    workspace_name: str = ""
    app_id: str = ""
    app_name: str = ""
    total: float = 0.0
    buckets: tuple[AccountActivityBucketResponse, ...] = ()


class AccountActivityResponse(HttpModel):
    """An account's activity over a window, split by the app it belongs to.

    `unit` says what every figure here is denominated in; `measure` says which
    question was asked. `total` reads the whole window in that same unit — a
    count of starts, or the level held averaged across the window — so a series
    total is comparable against it and the shares still add up.

    No workspace on the envelope: this covers an account, and naming one of its
    workspaces would state a scope the response does not have.
    """

    measure: AccountActivityMeasure
    unit: AccountActivityUnit
    window_seconds: int
    start: datetime
    end: datetime
    total: float = 0.0
    series: tuple[AccountActivitySeriesResponse, ...] = ()


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
    "ACTIVITY_MEASURE_UNITS",
    "AccountActivityBucketResponse",
    "AccountActivityMeasure",
    "AccountActivityResponse",
    "AccountActivitySeriesKind",
    "AccountActivitySeriesResponse",
    "AccountActivityUnit",
    "AccountContainerCountsResponse",
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
]
