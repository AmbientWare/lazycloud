from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator
from shared.checkpoints import CheckpointRecord
from shared.container_requests import StopContainerReason
from shared.contracts import ContractModel
from shared.identity import AuthTokenRecord, TokenKind
from shared.image_building.credentials import normalize_registry_host
from shared.realtime.contracts import CloudEventRecord, ContainerMetricsPayload
from shared.scheduling import (
    ContainerIpAssignment,
    ContainerStatusUpdatePlan,
    NetworkIpMutationPlan,
    SchedulerBackendRoute,
    SchedulerContainerAddress,
    SchedulerContainerAddressMap,
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    WorkerCapacityChange,
    WorkerCapacityPlan,
    WorkerRemovalResult,
    WorkerRepositoryLockRecord,
    WorkerRepositoryLockRelease,
)
from shared.source_cache_cleanup import (
    SourceCacheCleanupTargetRecord,
    WorkerCacheGenerationState,
)
from shared.usage import UsageRecord
from shared.worker_events import WorkerEventRecord

from worker.checkpoints import CheckpointStatePayload
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.events import (
    ContainerEventPayload,
    ContainerExecutionPhase,
    ContainerLifecyclePayload,
)
from worker.origin_access import CacheOriginCredentials, ImageArchiveUploadCredentials
from worker.sandbox_server import SandboxProcessLogEntry
from worker.tools import ContainerCredentials

DEFAULT_WORKER_EVENT_HEARTBEAT_SECONDS = 30.0
DEFAULT_WORKER_EVENT_PUBSUB_TIMEOUT_SECONDS = 1.0
MAX_CONTAINER_LOG_BATCH_ENTRIES = 128
MAX_CONTAINER_LOG_MESSAGE_BYTES = 16_384


class WorkerRepositoryPrincipal(ContractModel):
    workspace_id: str = ""
    token_kind: TokenKind = TokenKind.Worker
    token_id: str = ""
    worker_id: str = ""

    @classmethod
    def from_token(cls, token: AuthTokenRecord) -> WorkerRepositoryPrincipal:
        return cls(
            workspace_id=token.workspace_id,
            token_kind=token.kind,
            token_id=token.id,
            worker_id=token.worker_id,
        )

    def credential_principal(
        self,
        *,
        proven_workspace_id: str | None = None,
    ) -> WorkerCredentialPrincipal:
        return WorkerCredentialPrincipal(
            workspace_id=(
                self.workspace_id if proven_workspace_id is None else proven_workspace_id
            ),
            token_kind=self.token_kind,
            token_id=self.token_id,
        )

    @property
    def is_managed_worker(self) -> bool:
        return self.token_kind is TokenKind.Worker

    @property
    def is_private_worker(self) -> bool:
        return self.token_kind is TokenKind.WorkerPrivate


class WorkerRepositoryResponse(ContractModel):
    """Marker base for worker repository response payloads."""


class GetNextContainerRequestRequest(ContractModel):
    worker_id: str
    cache_generation_id: str
    cache_session_fence: int = Field(ge=1)
    max_responses: int = 0


class GetNextContainerRequestResponse(WorkerRepositoryResponse):
    container_request: SchedulerWorkerRequest | None = None


class StreamWorkerEventsRequest(ContractModel):
    worker_id: str = ""
    event_ids: list[str] = Field(default_factory=list)
    heartbeat_interval_seconds: float = DEFAULT_WORKER_EVENT_HEARTBEAT_SECONDS
    pubsub_timeout_seconds: float = DEFAULT_WORKER_EVENT_PUBSUB_TIMEOUT_SECONDS
    max_events: int = 0


class AcknowledgeWorkerEventRequest(ContractModel):
    event_id: str
    worker_id: str


class AcknowledgeWorkerEventResponse(WorkerRepositoryResponse):
    acknowledged: bool = False


class WorkerIdRequest(ContractModel):
    worker_id: str


class DisableWorkerRequest(ContractModel):
    worker_id: str
    reason: str


class AddWorkerRequest(ContractModel):
    worker: SchedulerWorkerRecord
    cache_generation_id: str
    cache_storage_id: str
    ttl_seconds: int = 0


class WorkerCacheSession(ContractModel):
    generation_id: str
    session_fence: int = Field(ge=1)


class WorkerCacheSessionRequest(ContractModel):
    worker_id: str
    cache_generation_id: str
    cache_session_fence: int = Field(ge=1)


class ClaimSourceCacheCleanupRequest(WorkerCacheSessionRequest):
    limit: int = Field(default=128, ge=1, le=512)


class ClaimSourceCacheCleanupResponse(WorkerRepositoryResponse):
    targets: list[SourceCacheCleanupTargetRecord] = Field(default_factory=list)


class ResolveSourceCacheCleanupRequest(WorkerCacheSessionRequest):
    target_id: str
    claim_token: str


class ResolveSourceCacheCleanupResponse(WorkerRepositoryResponse):
    resolved: bool


class SetImagePullLockRequest(ContractModel):
    worker_id: str
    image_id: str
    ttl_seconds: int = 30
    retries: int = 600


class SetImagePullLockResponse(WorkerRepositoryResponse):
    lock: WorkerRepositoryLockRecord | None = None


class RemoveImagePullLockRequest(ContractModel):
    worker_id: str
    image_id: str
    token: str


class RemoveImagePullLockResponse(WorkerRepositoryResponse):
    release: WorkerRepositoryLockRelease | None = None


class WorkerContainerIndexRequest(ContractModel):
    worker_id: str
    container_id: str


class WorkerContainerIndexResponse(WorkerRepositoryResponse):
    count: int = 0


class GetWorkerByIdResponse(WorkerRepositoryResponse):
    worker: SchedulerWorkerRecord | None = None


class WorkerRecordResponse(WorkerRepositoryResponse):
    worker: SchedulerWorkerRecord | None = None
    worker_session_token: str = ""
    cache_session: WorkerCacheSession | None = None


class WorkerKeepAliveResponse(WorkerRepositoryResponse):
    worker: SchedulerWorkerRecord | None = None
    source_cache_state: WorkerCacheGenerationState


class RemoveWorkerResponse(WorkerRepositoryResponse):
    removal: WorkerRemovalResult | None = None


class UpdateWorkerCapacityRequest(ContractModel):
    worker_id: str
    container_request: SchedulerWorkerRequest
    change: WorkerCapacityChange


class UpdateWorkerCapacityResponse(WorkerRepositoryResponse):
    plan: WorkerCapacityPlan | None = None


class UpdateContainerStatusRequest(ContractModel):
    container_id: str
    status: SchedulerContainerStatus
    ttl_seconds: int = 900


class UpdateContainerStatusResponse(WorkerRepositoryResponse):
    state: SchedulerContainerState | None = None
    plan: ContainerStatusUpdatePlan | None = None


class SetContainerExitCodeRequest(ContractModel):
    container_id: str
    exit_code: int
    termination_reason: StopContainerReason = StopContainerReason.Unknown
    # A container that died before or during its run phase carries the reason on the
    # same synchronous call that makes its task terminal. The asynchronous lifecycle
    # event also reports it, but arrives after the task is already terminal and is
    # dropped, so it cannot be the only carrier.
    failed_phase: ContainerExecutionPhase | None = None
    failure_detail: str = Field(default="", max_length=2000)
    ttl_seconds: int = 86_400


class SetContainerExitCodeResponse(WorkerRepositoryResponse):
    container_id: str = ""


class GetContainerStateRequest(ContractModel):
    container_id: str


class GetContainerStateResponse(WorkerRepositoryResponse):
    state: SchedulerContainerState | None = None


class DeleteContainerStateRequest(ContractModel):
    container_id: str


class DeleteContainerStateResponse(WorkerRepositoryResponse):
    deleted: bool = False


class SetWorkerAddressRequest(ContractModel):
    container_id: str
    address: str
    route: SchedulerBackendRoute | None = None


class SetWorkerAddressResponse(WorkerRepositoryResponse):
    address: SchedulerContainerAddress | None = None


class SetContainerAddressRequest(ContractModel):
    container_id: str
    address: str
    route: SchedulerBackendRoute | None = None


class SetContainerAddressResponse(WorkerRepositoryResponse):
    address: SchedulerContainerAddress | None = None


class SetContainerAddressMapRequest(ContractModel):
    container_id: str
    address_map: dict[int, str] = Field(default_factory=dict)
    routes: list[SchedulerBackendRoute] = Field(default_factory=list)


class SetContainerAddressMapResponse(WorkerRepositoryResponse):
    address_map: SchedulerContainerAddressMap | None = None


class GetContainerAddressRequest(ContractModel):
    container_id: str


class GetContainerAddressResponse(WorkerRepositoryResponse):
    address: SchedulerContainerAddress | None = None


class GetWorkerAddressRequest(ContractModel):
    container_id: str


class GetWorkerAddressResponse(WorkerRepositoryResponse):
    address: SchedulerContainerAddress | None = None


class GetContainerAddressMapRequest(ContractModel):
    container_id: str


class GetContainerAddressMapResponse(WorkerRepositoryResponse):
    address_map: SchedulerContainerAddressMap | None = None


class GetCacheOriginCredentialsResponse(WorkerRepositoryResponse):
    credentials: CacheOriginCredentials | None = None


class GetImageArchiveUploadCredentialsResponse(WorkerRepositoryResponse):
    credentials: ImageArchiveUploadCredentials | None = None


class ImageBuildRegistryAuth(ContractModel):
    registry: str
    auth: str = Field(default="", repr=False)
    identity_token: str = Field(default="", repr=False)

    @field_validator("registry")
    @classmethod
    def require_registry(cls, value: str) -> str:
        registry = normalize_registry_host(value)
        if not registry:
            raise ValueError("image build registry auth requires a valid registry")
        return registry

    @model_validator(mode="after")
    def require_single_auth_value(self) -> ImageBuildRegistryAuth:
        if bool(self.auth) == bool(self.identity_token):
            raise ValueError("image build registry auth requires exactly one auth value")
        return self


class ImageBuildPrivateInputs(ContractModel):
    registry_auth: ImageBuildRegistryAuth | None = Field(default=None, repr=False)
    build_args: dict[str, str] = Field(default_factory=dict, repr=False)

    @property
    def empty(self) -> bool:
        return self.registry_auth is None and not self.build_args


class GetImageBuildCredentialsRequest(ContractModel):
    workspace_id: str
    build_id: str
    container_id: str
    registry: str
    cache_key: str


class GetImageBuildCredentialsResponse(WorkerRepositoryResponse):
    private_inputs: ImageBuildPrivateInputs | None = Field(default=None, repr=False)


class PrepareImageBuildContextDownloadRequest(ContractModel):
    workspace_id: str
    build_id: str
    container_id: str
    object_id: str


class PrepareImageBuildContextDownloadResponse(WorkerRepositoryResponse):
    object_id: str = ""
    download_url: str = Field(default="", repr=False)
    content_length: int = Field(default=0, ge=0)
    sha256: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    expires_at: datetime | None = None


class GetContainerCredentialsResponse(WorkerRepositoryResponse):
    credentials: ContainerCredentials | None = None


class SaveCheckpointStateRequest(ContractModel):
    payload: CheckpointStatePayload


class SaveCheckpointStateResponse(WorkerRepositoryResponse):
    checkpoint: CheckpointRecord | None = None


class AcquireAutomaticCheckpointLeaseRequest(ContractModel):
    workspace_id: str
    stub_id: str
    container_id: str
    ttl_seconds: int = Field(ge=1)


class AcquireAutomaticCheckpointLeaseResponse(WorkerRepositoryResponse):
    acquired: bool = False
    available_checkpoint_id: str = ""


class ReleaseAutomaticCheckpointLeaseRequest(ContractModel):
    workspace_id: str
    stub_id: str
    container_id: str


class ReleaseAutomaticCheckpointLeaseResponse(WorkerRepositoryResponse):
    released: bool = False


class GetCheckpointRestoreRequest(ContractModel):
    checkpoint_id: str
    workspace_id: str
    checkpoint_bucket: str


class GetCheckpointRestoreResponse(WorkerRepositoryResponse):
    checkpoint: CheckpointRecord | None = None
    download_url: str = Field(default="", repr=False)


class PrepareCheckpointArchiveUploadRequest(ContractModel):
    checkpoint_id: str
    origin_key: str
    cache_hash: str
    cache_size_bytes: int
    checkpoint_bucket: str


class PrepareCheckpointArchiveUploadResponse(WorkerRepositoryResponse):
    upload_url: str = Field(default="", repr=False)


class PersistCheckpointArchiveRequest(ContractModel):
    checkpoint_id: str
    origin_key: str
    cache_hash: str
    cache_size_bytes: int
    checkpoint_bucket: str
    cache_namespace: str
    locality: str = ""
    accelerator: str = ""


class PersistCheckpointArchiveResponse(WorkerRepositoryResponse):
    checkpoint_id: str = ""
    origin_key: str = ""
    cache_hash: str = ""
    cache_size_bytes: int = 0
    locality: str = ""
    accelerator: str = ""


class PublishWorkerEventRequest(ContractModel):
    record: WorkerEventRecord


class PublishWorkerEventResponse(WorkerRepositoryResponse):
    record: WorkerEventRecord | None = None


class RecordWorkerUsageRequest(ContractModel):
    record: UsageRecord


class RecordWorkerUsageResponse(WorkerRepositoryResponse):
    record: UsageRecord | None = None


class PublishContainerLifecycleRequest(ContractModel):
    payload: ContainerLifecyclePayload


class PublishContainerLifecycleResponse(WorkerRepositoryResponse):
    event: CloudEventRecord | None = None


class PublishContainerMetricsRequest(ContractModel):
    payload: ContainerMetricsPayload


class PublishContainerMetricsResponse(WorkerRepositoryResponse):
    event: CloudEventRecord | None = None


class PublishContainerEventRequest(ContractModel):
    payload: ContainerEventPayload


class PublishContainerEventResponse(WorkerRepositoryResponse):
    event: CloudEventRecord | None = None


class AppendSandboxProcessLogRequest(ContractModel):
    entry: SandboxProcessLogEntry


class AppendSandboxProcessLogResponse(WorkerRepositoryResponse):
    event: CloudEventRecord | None = None


class ContainerLogStream(StrEnum):
    Stdout = "stdout"
    Stderr = "stderr"


class ContainerLogEntryKind(StrEnum):
    Output = "output"
    Dropped = "dropped"
    Flush = "flush"
    # Worker-authored, never container output: why a container produced none.
    Diagnostic = "diagnostic"


class ContainerLogBatchEntry(ContractModel):
    sequence: int = Field(ge=0)
    stream: ContainerLogStream
    message: str = ""
    timestamp: datetime
    kind: ContainerLogEntryKind = ContainerLogEntryKind.Output
    dropped_count: int = Field(default=0, ge=0)

    @field_validator("message")
    @classmethod
    def message_must_fit_ingestion_boundary(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_CONTAINER_LOG_MESSAGE_BYTES:
            raise ValueError(
                f"container log message exceeds {MAX_CONTAINER_LOG_MESSAGE_BYTES} bytes"
            )
        return value

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("container log timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def fields_must_match_entry_kind(self) -> ContainerLogBatchEntry:
        if self.kind is ContainerLogEntryKind.Output and not self.message:
            raise ValueError("container output log message must not be empty")
        if self.kind is ContainerLogEntryKind.Diagnostic and not self.message:
            raise ValueError("diagnostic container log entry must include a message")
        if self.kind is ContainerLogEntryKind.Dropped and self.dropped_count <= 0:
            raise ValueError("dropped container log entry must include dropped_count")
        if self.kind is not ContainerLogEntryKind.Dropped and self.dropped_count:
            raise ValueError("dropped_count is only valid for dropped container log entries")
        return self


class AppendContainerLogsRequest(ContractModel):
    container_id: str
    capture_id: str
    entries: list[ContainerLogBatchEntry] = Field(
        min_length=1,
        max_length=MAX_CONTAINER_LOG_BATCH_ENTRIES,
    )

    @field_validator("container_id", "capture_id")
    @classmethod
    def identifiers_must_be_bounded(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("container log identifiers must not be empty")
        if len(normalized) > 160:
            raise ValueError("container log identifiers must not exceed 160 characters")
        return normalized

    @model_validator(mode="after")
    def entries_must_be_contiguous(self) -> AppendContainerLogsRequest:
        expected = self.entries[0].sequence
        for entry in self.entries:
            if entry.sequence != expected:
                raise ValueError("container log batch sequences must be contiguous and ordered")
            expected += 1
        return self


class AppendContainerLogsResponse(WorkerRepositoryResponse):
    accepted_through: int = Field(default=-1, ge=-1)
    appended_count: int = Field(default=0, ge=0)


class NetworkLockRequest(ContractModel):
    ttl_seconds: int = 30
    retries: int = 0


class NetworkLockResponse(WorkerRepositoryResponse):
    lock: WorkerRepositoryLockRecord | None = None


class RemoveNetworkLockRequest(ContractModel):
    token: str


class RemoveNetworkLockResponse(WorkerRepositoryResponse):
    release: WorkerRepositoryLockRelease | None = None


class SetContainerIpRequest(ContractModel):
    container_id: str
    ip_address: str


class SetContainerIpResponse(WorkerRepositoryResponse):
    plan: NetworkIpMutationPlan | None = None


class MoveContainerIpRequest(ContractModel):
    from_container_id: str
    to_container_id: str
    ip_address: str


class MoveContainerIpResponse(WorkerRepositoryResponse):
    plan: NetworkIpMutationPlan | None = None


class GetContainerIpRequest(ContractModel):
    container_id: str


class GetContainerIpResponse(WorkerRepositoryResponse):
    ip_address: str = ""


class GetContainerIpsRequest(ContractModel):
    pass


class GetContainerIpsResponse(WorkerRepositoryResponse):
    ip_addresses: tuple[str, ...] = ()


class GetContainerIpAssignmentsRequest(ContractModel):
    pass


class GetContainerIpAssignmentsResponse(WorkerRepositoryResponse):
    assignments: tuple[ContainerIpAssignment, ...] = ()


class RemoveContainerIpRequest(ContractModel):
    container_id: str


class RemoveContainerIpResponse(WorkerRepositoryResponse):
    plan: NetworkIpMutationPlan | None = None
