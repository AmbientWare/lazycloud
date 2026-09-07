from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from pydantic import Field, field_validator

from shared.bytes_transport import EncodedBytesBody
from shared.enums import StringEnum
from shared.http.base import HttpModel


class CreatePodRequest(HttpModel):
    stub_id: str = ""
    image_id: str | None = None
    checkpoint_id: str | None = None
    command: list[str] | None = None
    timeout_seconds: int | None = Field(default=None, ge=-1)
    external_url: str = ""

    @field_validator("external_url")
    @classmethod
    def external_url_must_be_absolute(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            msg = "external_url must include a scheme and host"
            raise ValueError(msg)
        return value.rstrip("/")


class CreatePodResponse(HttpModel):
    container_id: str = ""
    stub_id: str = ""
    url: str = ""
    timeout_seconds: int = 0
    expires_at: datetime | None = None


class PodSandboxExecRequest(HttpModel):
    command: str
    cwd: str = "."
    env: dict[str, str] = Field(default_factory=dict)


class PodSandboxExecResponse(HttpModel):
    pid: int = 0


class PodSandboxStatusResponse(HttpModel):
    status: str = ""
    exit_code: int = 0


class PodSandboxStdoutResponse(HttpModel):
    stdout: str = ""


class PodSandboxStderrResponse(HttpModel):
    stderr: str = ""


class PodSandboxKillRequest(HttpModel):
    pid: int


class PodSandboxKillResponse(HttpModel):
    pass


class PodSandboxUploadFileBody(EncodedBytesBody):
    container_path: str
    mode: int = 0o644

    @property
    def data(self) -> bytes:
        return self.bytes_value()


class PodSandboxUploadFileResponse(HttpModel):
    pass


class PodSandboxDownloadFileResponse(EncodedBytesBody):
    @property
    def data(self) -> bytes:
        return self.bytes_value()

    @classmethod
    def from_bytes(cls, value: bytes) -> PodSandboxDownloadFileResponse:
        return cls(value_base64=EncodedBytesBody.from_bytes(value).value_base64)


class PodSandboxFileInfo(HttpModel):
    mode: int = 0
    size: int = 0
    mod_time: datetime | None = None
    owner: str = ""
    group: str = ""
    is_dir: bool = False
    name: str = ""
    permissions: int = 0


class PodSandboxListFilesResponse(HttpModel):
    files: list[PodSandboxFileInfo] = Field(default_factory=list)


class PodSandboxDeleteFileResponse(HttpModel):
    pass


class PodSandboxCreateDirectoryRequest(HttpModel):
    container_path: str
    mode: int = 0o755


class PodSandboxCreateDirectoryResponse(HttpModel):
    pass


class PodSandboxDeleteDirectoryResponse(HttpModel):
    pass


class PodSandboxStatFileResponse(HttpModel):
    file_info: PodSandboxFileInfo = PodSandboxFileInfo()


class PodSandboxReplaceInFilesRequest(HttpModel):
    container_path: str
    pattern: str
    new_string: str


class PodSandboxReplaceInFilesResponse(HttpModel):
    pass


class PodSandboxExposePortRequest(HttpModel):
    port: int = Field(strict=True, ge=1, le=65535)


class PodSandboxExposePortResponse(HttpModel):
    url: str = ""


class PodSandboxUpdateNetworkPermissionsRequest(HttpModel):
    block_network: bool = False
    allow_list: list[str] = Field(default_factory=list)


class PodSandboxUpdateNetworkPermissionsResponse(HttpModel):
    block_network: bool = False
    allow_list: list[str] = Field(default_factory=list)


class PodFileSearchMatch(HttpModel):
    path: str
    text: str = ""
    line: int = 0
    column: int = 0


class PodSandboxFindInFilesRequest(HttpModel):
    container_path: str
    pattern: str


class PodSandboxFindInFilesResponse(HttpModel):
    results: list[PodFileSearchMatch] = Field(default_factory=list)


class PodSandboxConnectResponse(HttpModel):
    stub_id: str = ""


class PodSandboxUpdateTTLRequest(HttpModel):
    ttl: int = Field(ge=-1)


class PodSandboxUpdateTTLResponse(HttpModel):
    ttl: int = Field(ge=-1)
    expires_at: datetime | None = None


class PodSandboxCreateImageFromFilesystemRequest(HttpModel):
    pass


class PodSandboxCreateImageFromFilesystemResponse(HttpModel):
    image_id: str = ""


class PodSandboxSnapshotMemoryRequest(HttpModel):
    pass


class PodSandboxSnapshotMemoryResponse(HttpModel):
    checkpoint_id: str = ""


class PodSandboxProcessInfo(HttpModel):
    pid: int
    command: str


class PodSandboxListProcessesResponse(HttpModel):
    processes: list[PodSandboxProcessInfo] = Field(default_factory=list)


class PodSandboxListUrlsResponse(HttpModel):
    urls: dict[int, str] = Field(default_factory=dict)


class SandboxDashboardStatus(StringEnum):
    Pending = "pending"
    Running = "running"
    Stopping = "stopping"
    Stopped = "stopped"
    Failed = "failed"


class SandboxRow(HttpModel):
    id: str
    stub_id: str | None = None
    name: str
    created_at: datetime
    status: SandboxDashboardStatus
    gpu: list[str] = Field(default_factory=list)
    container_id: str | None = None
    time_to_started_ms: int | None = None
    lifetime_ms: int | None = None


class SandboxListRequest(HttpModel):
    workspace: str = "default"
    app_id: str | None = None
    limit: int = 50


class SandboxListResponse(HttpModel):
    data: tuple[SandboxRow, ...]
    next: str = ""


class SandboxCreatedBucket(HttpModel):
    timestamp: datetime
    count: int


class SandboxStatsRequest(HttpModel):
    workspace: str = "default"
    app_id: str | None = None


class SandboxStatsResponse(HttpModel):
    concurrent: int
    total_created: int
    rate_per_second: float
    status_counts: dict[SandboxDashboardStatus, int]
    created_buckets: tuple[SandboxCreatedBucket, ...] = ()


class SandboxTimelineRequest(HttpModel):
    workspace: str = "default"
    stub_id: str
    container_id: str | None = None


class SandboxTimeline(HttpModel):
    container_id: str | None = None
    status: SandboxDashboardStatus
    created_at: datetime
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    scheduling_ms: int | None = None
    startup_ms: int | None = None
    runtime_ms: int | None = None


__all__ = [
    "CreatePodRequest",
    "CreatePodResponse",
    "PodFileSearchMatch",
    "PodSandboxConnectResponse",
    "PodSandboxCreateDirectoryRequest",
    "PodSandboxCreateDirectoryResponse",
    "PodSandboxCreateImageFromFilesystemRequest",
    "PodSandboxCreateImageFromFilesystemResponse",
    "PodSandboxDeleteDirectoryResponse",
    "PodSandboxDeleteFileResponse",
    "PodSandboxDownloadFileResponse",
    "PodSandboxExecRequest",
    "PodSandboxExecResponse",
    "PodSandboxExposePortRequest",
    "PodSandboxExposePortResponse",
    "PodSandboxFileInfo",
    "PodSandboxFindInFilesRequest",
    "PodSandboxFindInFilesResponse",
    "PodSandboxKillRequest",
    "PodSandboxKillResponse",
    "PodSandboxListFilesResponse",
    "PodSandboxListProcessesResponse",
    "PodSandboxListUrlsResponse",
    "PodSandboxReplaceInFilesRequest",
    "PodSandboxReplaceInFilesResponse",
    "PodSandboxSnapshotMemoryRequest",
    "PodSandboxSnapshotMemoryResponse",
    "PodSandboxStatFileResponse",
    "PodSandboxStatusResponse",
    "PodSandboxStderrResponse",
    "PodSandboxStdoutResponse",
    "PodSandboxUpdateNetworkPermissionsRequest",
    "PodSandboxUpdateNetworkPermissionsResponse",
    "PodSandboxUpdateTTLRequest",
    "PodSandboxUpdateTTLResponse",
    "PodSandboxUploadFileBody",
    "PodSandboxUploadFileResponse",
    "SandboxCreatedBucket",
    "SandboxDashboardStatus",
    "SandboxListRequest",
    "SandboxListResponse",
    "SandboxRow",
    "SandboxStatsRequest",
    "SandboxStatsResponse",
    "SandboxTimeline",
    "SandboxTimelineRequest",
]
