from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import Field
from shared.contracts import ContractModel
from shared.image_building.authoring import ImageBuildStepKind
from shared.image_building.records import BuildStatus, ImageBuildPhase

type BaseImageDigestInspector = Callable[[str, str], str]


class BaseImageDigestResolutionStatus(StrEnum):
    Skipped = "skipped"
    CacheHit = "cache-hit"
    Resolved = "resolved"
    MissingDigest = "missing-digest"
    InspectFailed = "inspect-failed"


class ImageInstallCommandMode(StrEnum):
    Dockerfile = "dockerfile"
    DockerfileManagedPython = "dockerfile-managed-python"
    Runtime = "runtime"


class ImageBuildWorkReason(StrEnum):
    ArchivePublication = "archive-publication"
    Commands = "commands"
    BuildSteps = "build-steps"
    PythonPackages = "python-packages"
    Environment = "environment"
    Secrets = "secrets"
    PythonRuntime = "python-runtime"


class PythonRuntimeSetupAction(StrEnum):
    Skipped = "skipped"
    UseBasePython = "use-base-python"
    InstallManagedPython = "install-managed-python"
    ConfigureMicromamba = "configure-micromamba"


class ImageBuildLifecycleAction(StrEnum):
    Noop = "noop"
    Wait = "wait"
    MarkRunning = "mark-running"
    Complete = "complete"
    Fail = "fail"
    SendStopEvent = "send-stop-event"
    DeletePendingState = "delete-pending-state"
    MarkStopping = "mark-stopping"
    KillContainer = "kill-container"
    StopExpiredContainer = "stop-expired-container"


class ImageBuildWaitOutcome(StrEnum):
    Continue = "continue"
    Running = "running"
    Complete = "complete"
    Failed = "failed"
    Aborted = "aborted"
    Timeout = "timeout"


class ImageBuildTtlEventKind(StrEnum):
    SchedulerStateSet = "scheduler-state-set"
    BuildTtlExpired = "build-ttl-expired"


class ImageBuildKeyEventOperation(StrEnum):
    Set = "set"
    Expired = "expired"
    Other = "other"


class ImageBuildTtlKeyFamily(StrEnum):
    BuildContainerTtl = "build-container-ttl"
    SchedulerContainerState = "scheduler-container-state"
    Unknown = "unknown"


class ImageBuildSpinupTimeoutReason(StrEnum):
    Default = "default"
    Dockerfile = "dockerfile"
    SourceImageSize = "source-image-size"
    SourceImageInspectFailed = "source-image-inspect-failed"


class ImageRegistryCredentialKind(StrEnum):
    Public = "public"
    Basic = "basic"
    Aws = "aws"
    Gcp = "gcp"
    Azure = "azure"
    Token = "token"
    Unknown = "unknown"


class ImageBuildCredentialAction(StrEnum):
    Skip = "skip"
    UseSourcePullCredentials = "use-source-pull-credentials"


class ImageBuildStreamEventKind(StrEnum):
    Log = "log"
    Warning = "warning"
    Reused = "reused"
    Complete = "complete"
    Failed = "failed"
    Cancelled = "cancelled"
    Timeout = "timeout"


class ImageBuildSessionStep(StrEnum):
    StartContainer = "start-container"
    RefreshContainerTtl = "refresh-container-ttl"
    StreamLogs = "stream-logs"
    WaitForContainer = "wait-for-container"
    PrepareRuntimeCommands = "prepare-runtime-commands"
    ExecuteRuntimeCommands = "execute-runtime-commands"
    ArchiveFilesystem = "archive-filesystem"
    Complete = "complete"


class BaseImageDigestRequest(ContractModel):
    registry: str = ""
    name: str = ""
    tag: str = ""
    current_digest: str = ""
    credentials: str = Field(default="", repr=False)
    cacheable: bool = True

    @property
    def source_image(self) -> str:
        if not self.registry or not self.name or not self.tag:
            return ""
        return f"{self.registry.rstrip('/')}/{self.name.lstrip('/')}:{self.tag}"


class BaseImageDigestCacheEntry(ContractModel):
    source_image: str
    digest: str
    expires_at: datetime


class BaseImageDigestResolution(ContractModel):
    status: BaseImageDigestResolutionStatus
    source_image: str = ""
    digest: str = ""
    cacheable: bool = True
    shared_lookup: bool = False
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.digest != ""


class ImageSourceReference(ContractModel):
    registry: str
    repository: str
    tag: str = ""
    digest: str = ""

    @property
    def source_image(self) -> str:
        if self.digest:
            tag = f":{self.tag}" if self.tag else ""
            return f"{self.registry}/{self.repository}{tag}@{self.digest}"
        return f"{self.registry}/{self.repository}:{self.tag or 'latest'}"


class ImageBuildSourcePlan(ContractModel):
    source_image: str = ""
    reference: ImageSourceReference | None = None
    custom_dockerfile: bool = False
    reason: str = ""


class PythonRuntimeSetupPlan(ContractModel):
    action: PythonRuntimeSetupAction
    requires_python: bool
    python_version: str = ""
    python_executable: str = "python"
    dockerfile_instructions: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    reason: str = ""


class ImageBuildCancellationPlan(ContractModel):
    actions: list[ImageBuildLifecycleAction] = Field(default_factory=list)
    stop_build_event: bool = False
    delete_pending_state: bool = False
    mark_stopping: bool = False
    kill_container: bool = False
    reason: str = ""


class ImageBuildTtlPlan(ContractModel):
    action: ImageBuildLifecycleAction = ImageBuildLifecycleAction.Noop
    stop_container: bool = False
    container_id: str = ""
    reason: str = ""


class ImageBuildTtlKeyEventPlan(ContractModel):
    family: ImageBuildTtlKeyFamily = ImageBuildTtlKeyFamily.Unknown
    event_kind: ImageBuildTtlEventKind | None = None
    container_id: str = ""
    relevant: bool = False
    ttl_plan: ImageBuildTtlPlan = Field(default_factory=ImageBuildTtlPlan)
    reason: str = ""


class ImageBuildSpinupTimeoutPlan(ContractModel):
    timeout_seconds: int
    reason: ImageBuildSpinupTimeoutReason
    source_image_size_bytes: int = 0
    archive_nanoseconds_per_byte: int = 0


class ImageBuildWaitPlan(ContractModel):
    outcome: ImageBuildWaitOutcome
    action: ImageBuildLifecycleAction
    terminal: bool = False
    status: BuildStatus = BuildStatus.Running
    phase: ImageBuildPhase = ImageBuildPhase.Submitted
    message: str = ""
    stop_container: bool = False
    reason: str = ""


class ImageBuildStreamEventPlan(ContractModel):
    kind: ImageBuildStreamEventKind
    image_id: str = ""
    build_id: str = ""
    message: str = ""
    done: bool = False
    success: bool = False
    python_version: str = ""
    warning: bool = False
    status: BuildStatus = BuildStatus.Running
    phase: ImageBuildPhase = ImageBuildPhase.Submitted
    error: str = ""


class ImageBuildSessionPlan(ContractModel):
    clip_version: int = 2
    image_id: str = ""
    build_id: str = ""
    container_id: str = ""
    build_container_required: bool = True
    v2: bool = True
    steps: list[ImageBuildSessionStep] = Field(default_factory=list)
    work_reasons: list[ImageBuildWorkReason] = Field(default_factory=list)
    spinup_timeout: ImageBuildSpinupTimeoutPlan | None = None
    cancellation: ImageBuildCancellationPlan = Field(default_factory=ImageBuildCancellationPlan)
    stream_events: list[ImageBuildStreamEventPlan] = Field(default_factory=list)
    ttl_seconds: int = 0
    keepalive_interval_seconds: int = 0


class ImageRegistryCredentialPayload(ContractModel):
    registry: str
    kind: ImageRegistryCredentialKind = ImageRegistryCredentialKind.Public
    credentials: dict[str, str] = Field(default_factory=dict, repr=False)

    @property
    def has_credentials(self) -> bool:
        return bool(self.credentials) and self.kind is not ImageRegistryCredentialKind.Public

    @property
    def credential_keys(self) -> list[str]:
        return sorted(self.credentials)


class ImageBuildCredentialPlan(ContractModel):
    action: ImageBuildCredentialAction = ImageBuildCredentialAction.Skip
    registry: str = ""
    kind: ImageRegistryCredentialKind = ImageRegistryCredentialKind.Public
    credential_keys: list[str] = Field(default_factory=list)
    use_source_pull_credentials: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ImageBuildCommand:
    kind: ImageBuildStepKind
    command: str
    args: tuple[str, ...] = ()
    isolated: bool = False


@dataclass(frozen=True, slots=True)
class ImageBuildWorkPlan:
    has_work: bool
    reasons: tuple[ImageBuildWorkReason, ...] = ()
