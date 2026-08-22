from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from shared.timestamps import utc_now


class BenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BenchmarkKind(StrEnum):
    Startup = "startup"
    Cache = "cache"
    Sandbox = "sandbox"
    Filesystem = "filesystem"
    Image = "image"
    Full = "full"


class BenchmarkRunnerKind(StrEnum):
    Startup = "startup"
    Cache = "cache"
    Sandbox = "sandbox"
    Composite = "composite"
    ImageLayer = "image_layer"


class BenchmarkStatus(StrEnum):
    Passed = "passed"
    Failed = "failed"


class BenchmarkAccess(StrEnum):
    VolumeMount = "volume_mount"
    WorkspaceFuse = "workspace_fuse"


class BenchmarkOperation(StrEnum):
    Read = "read"
    Write = "write"


class BenchmarkReadPattern(StrEnum):
    Sequential = "sequential"
    Random = "random"


class BenchmarkCacheState(StrEnum):
    StrictDisk = "strict_disk"
    Hot = "hot"
    Cold = "cold"
    AfterWorkerRestart = "after_worker_restart"


class BenchmarkProbe(StrEnum):
    SandboxRead = "sandbox_read"
    WorkerRead = "worker_read"
    WorkerDirectRead = "worker_direct_read"
    SandboxDirectRead = "sandbox_direct_read"
    RemoteCacheSocket = "remote_cache_socket"
    CachePath = "cache_path"
    StartupEvents = "startup_events"
    ImageLayerEvents = "image_layer_events"
    ImageCacheEvents = "image_cache_events"
    WorkerRestart = "worker_restart"
    EmbeddedCachePageProof = "embedded_cache_page_proof"


class BenchmarkMeasurementStatus(StrEnum):
    Ok = "ok"
    Error = "error"


class BenchmarkMeasurementName(StrEnum):
    ProcessStartup = "process_startup"
    LocalCacheRoundtrip = "local_cache_roundtrip"
    SandboxFilesystem = "sandbox_filesystem"
    PythonFileRead = "python_file_read"
    WorkerFileRead = "worker_file_read"
    RemoteCacheSocketRead = "remote_cache_socket_read"
    CachePathProof = "cache_path_proof"
    ImageLayerRead = "image_layer_read"
    Generic = "generic"


class BenchmarkThresholds(BenchmarkModel):
    python_file_read_min_mbps: float | None = None
    remote_cache_socket_read_min_mbps: float | None = None
    min_mbps: float | None = None


class BenchmarkValidationPolicy(BenchmarkModel):
    requires_sha: bool = False
    requires_cache_hit: bool = False
    requires_remote_read: bool = False
    reject_cloud_read: bool = False
    min_mbps: float | None = None


class BenchmarkEvidence(BenchmarkModel):
    sha_ok: bool | None = None
    cache_hit: bool | None = None
    remote_worker: bool | None = None
    remote_node: bool | None = None
    cloud_read: bool | None = None
    cache_source: str | None = None
    artifact_output: str | None = None
    network_ceiling_mbps: float | None = None
    extra: dict[str, JsonValue] = Field(default_factory=dict)


class BenchmarkCase(BenchmarkModel):
    kind: BenchmarkKind
    name: str
    description: str


class BenchmarkResult(BenchmarkModel):
    case: BenchmarkCase
    status: BenchmarkStatus
    duration_ms: float
    metrics: dict[str, float] = Field(default_factory=dict)
    details: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = None


class BenchmarkReport(BenchmarkModel):
    results: list[BenchmarkResult]
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def passed(self) -> bool:
        return all(result.status == BenchmarkStatus.Passed for result in self.results)


class BenchmarkMeasurement(BenchmarkModel):
    suite: str
    scenario: str
    measurement: BenchmarkMeasurementName = BenchmarkMeasurementName.Generic
    status: BenchmarkMeasurementStatus = BenchmarkMeasurementStatus.Ok
    duration_ms: float = 0
    bytes_read: int = 0
    mbps: float = 0
    validation: BenchmarkValidationPolicy = Field(default_factory=BenchmarkValidationPolicy)
    evidence: BenchmarkEvidence = Field(default_factory=BenchmarkEvidence)
    tags: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)

    @field_validator("duration_ms", "mbps")
    @classmethod
    def _non_negative_float(cls, value: float) -> float:
        if value < 0:
            msg = "benchmark metric values must be non-negative"
            raise ValueError(msg)
        return value

    @field_validator("bytes_read")
    @classmethod
    def _non_negative_int(cls, value: int) -> int:
        if value < 0:
            msg = "benchmark byte counts must be non-negative"
            raise ValueError(msg)
        return value


class ScenarioSpec(BenchmarkModel):
    name: str
    access: BenchmarkAccess | None = None
    operation: BenchmarkOperation = BenchmarkOperation.Read
    pattern: BenchmarkReadPattern = BenchmarkReadPattern.Sequential
    size_mib: int = 0
    cache_state: BenchmarkCacheState | None = None
    probes: tuple[BenchmarkProbe, ...] = ()
    args: dict[str, JsonValue] = Field(default_factory=dict)
    thresholds: BenchmarkThresholds = Field(default_factory=BenchmarkThresholds)
    tags: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("size_mib")
    @classmethod
    def _size_mib_is_non_negative(cls, value: int) -> int:
        if value < 0:
            msg = "scenario size_mib must be non-negative"
            raise ValueError(msg)
        return value

    @property
    def file_plan_entry(self) -> str:
        if self.access is None or self.size_mib <= 0:
            return ""
        return f"{self.access.value}:{self.pattern.value}:{self.size_mib}"


class SuiteSpec(BenchmarkModel):
    name: str
    kind: BenchmarkKind
    runner: BenchmarkRunnerKind
    description: str = ""
    includes: tuple[str, ...] = ()
    defaults: dict[str, JsonValue] = Field(default_factory=dict)
    args: dict[str, JsonValue] = Field(default_factory=dict)
    scenarios: tuple[ScenarioSpec, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _fill_kind_and_runner(cls, data: JsonValue) -> JsonValue:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        name = str(values.get("name") or "")
        if "kind" not in values or values["kind"] in (None, ""):
            values["kind"] = name.split("-", 1)[0] if name else BenchmarkKind.Startup.value
        if "runner" not in values or values["runner"] in (None, ""):
            values["runner"] = values["kind"]
        return values

    @property
    def file_plan(self) -> str:
        entries = [scenario.file_plan_entry for scenario in self.scenarios]
        return ",".join(entry for entry in entries if entry)

    @property
    def scenario_names(self) -> tuple[str, ...]:
        return tuple(scenario.name for scenario in self.scenarios)
