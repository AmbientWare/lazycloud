from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse
from uuid import uuid4

from networking.internal_http import InternalHttpClient, InternalHttpError
from pydantic import JsonValue, TypeAdapter, ValidationError
from shared.checkpoints import AutomaticCheckpointCreationLease, CheckpointRecord
from shared.container_requests import StopContainerReason
from shared.contracts import ContractModel
from shared.image_building.records import BuildStatus
from shared.realtime.contracts import CloudEventRecord, ContainerMetricsPayload
from shared.routing import AgentBackendRoute
from shared.scheduling import (
    ContainerIpAssignment,
    ContainerStatusUpdatePlan,
    NetworkIpMutationPlan,
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
    WorkerUnavailableReason,
)
from shared.source_cache_cleanup import (
    SourceCacheCleanupTargetRecord,
    WorkerCacheGenerationState,
)
from shared.usage import UsageMetric, UsageRecord, UsageUnit
from shared.worker_events import WorkerEventRecord

from worker.checkpoints import CheckpointStatePayload
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.events import (
    ContainerExecutionPhase,
    ContainerLifecyclePayload,
    WorkerStreamEvent,
)
from worker.image_build_execution import WorkerImageBuildExecutionResult
from worker.origin_access import (
    CacheOriginCredentialRequest,
    ImageArchiveUploadCredentialRequest,
)
from worker.repository_payloads import (
    AcknowledgeContainerRequestRequest,
    AcknowledgeContainerRequestResponse,
    AcknowledgeWorkerEventRequest,
    AcknowledgeWorkerEventResponse,
    AcquireAutomaticCheckpointLeaseRequest,
    AcquireAutomaticCheckpointLeaseResponse,
    AddWorkerRequest,
    AppendContainerLogsRequest,
    AppendContainerLogsResponse,
    AppendSandboxProcessLogRequest,
    AppendSandboxProcessLogResponse,
    ClaimSourceCacheCleanupRequest,
    ClaimSourceCacheCleanupResponse,
    ContainerLogBatchEntry,
    DeleteContainerStateRequest,
    DeleteContainerStateResponse,
    DisableWorkerRequest,
    GetCacheOriginCredentialsResponse,
    GetCheckpointRestoreRequest,
    GetCheckpointRestoreResponse,
    GetContainerAddressMapRequest,
    GetContainerAddressMapResponse,
    GetContainerAddressRequest,
    GetContainerAddressResponse,
    GetContainerCredentialsResponse,
    GetContainerIpAssignmentsRequest,
    GetContainerIpAssignmentsResponse,
    GetContainerStateRequest,
    GetContainerStateResponse,
    GetImageArchiveUploadCredentialsResponse,
    GetImageBuildCredentialsRequest,
    GetImageBuildCredentialsResponse,
    GetNextContainerRequestRequest,
    GetNextContainerRequestResponse,
    GetWorkerAddressRequest,
    GetWorkerAddressResponse,
    GetWorkerByIdResponse,
    MoveContainerIpRequest,
    MoveContainerIpResponse,
    NetworkLockRequest,
    NetworkLockResponse,
    PersistCheckpointArchiveRequest,
    PersistCheckpointArchiveResponse,
    PrepareCheckpointArchiveUploadRequest,
    PrepareCheckpointArchiveUploadResponse,
    PrepareImageBuildContextDownloadRequest,
    PrepareImageBuildContextDownloadResponse,
    PublishContainerEventRequest,
    PublishContainerEventResponse,
    PublishContainerLifecycleRequest,
    PublishContainerLifecycleResponse,
    PublishContainerMetricsRequest,
    PublishContainerMetricsResponse,
    PublishWorkerEventRequest,
    PublishWorkerEventResponse,
    RecordWorkerUsageRequest,
    RecordWorkerUsageResponse,
    ReleaseAutomaticCheckpointLeaseRequest,
    ReleaseAutomaticCheckpointLeaseResponse,
    RemoveContainerIpRequest,
    RemoveContainerIpResponse,
    RemoveNetworkLockRequest,
    RemoveNetworkLockResponse,
    RemoveWorkerResponse,
    ReportImageBuildProgressRequest,
    ReportImageBuildProgressResponse,
    ReportImageBuildResultRequest,
    ReportImageBuildResultResponse,
    ResolveSourceCacheCleanupRequest,
    ResolveSourceCacheCleanupResponse,
    SaveCheckpointStateRequest,
    SaveCheckpointStateResponse,
    SetContainerAddressMapRequest,
    SetContainerAddressMapResponse,
    SetContainerAddressRequest,
    SetContainerAddressResponse,
    SetContainerExitCodeRequest,
    SetContainerExitCodeResponse,
    SetContainerIpRequest,
    SetContainerIpResponse,
    SetWorkerAddressRequest,
    SetWorkerAddressResponse,
    StreamWorkerEventsRequest,
    UpdateContainerStatusRequest,
    UpdateContainerStatusResponse,
    UpdateWorkerCapacityRequest,
    UpdateWorkerCapacityResponse,
    WorkerCacheSession,
    WorkerCacheSessionRequest,
    WorkerIdRequest,
    WorkerKeepAliveResponse,
    WorkerRecordResponse,
)
from worker.sandbox_server import SandboxProcessLogEntry
from worker.source_cache_cleanup import (
    WorkerSourceCacheClaimSource,
    WorkerSourceCacheIdentity,
    WorkerSourceCacheReconciler,
    WorkerSourceCacheReconcileResult,
    record_source_cache_session,
)
from worker.source_code import SourceCodePackageMaterializer
from worker.tools import ContainerCredentialRequest, ContainerCredentials

type JsonObject = dict[str, JsonValue]

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def _bounded_image_build_log(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= 8 * 1024:
        return value
    return encoded[: 8 * 1024].decode("utf-8", errors="ignore")


class WorkerRepositoryClientError(RuntimeError):
    pass


class WorkerSourceCacheNotAvailableError(WorkerRepositoryClientError):
    """The repository withheld the worker because its cache is not available.

    Distinct from a missing record: the worker is still registered, so
    registering it again changes nothing and only adds load.
    """

    def __init__(self, worker_id: str, state: WorkerCacheGenerationState) -> None:
        super().__init__(f"worker {worker_id!r} source cache is {state.value}")
        self.state = state


@dataclass(slots=True)
class WorkerRepositoryHttpTransport:
    """The worker's HTTP channel to the control plane."""

    endpoint: str
    token: str
    timeout_seconds: float = 30.0
    http: InternalHttpClient = field(default_factory=InternalHttpClient)

    def set_bearer_token(self, token: str) -> None:
        self.token = token

    def prepare_shutdown(self, *, timeout_seconds: float) -> None:
        self.timeout_seconds = min(self.timeout_seconds, max(timeout_seconds, 0.1))

    def post(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> JsonObject:
        try:
            response = self.http.request(
                "POST",
                self._url(path),
                headers=self._headers(),
                content=_encoded(payload),
                timeout_seconds=self.timeout_seconds,
            )
        except InternalHttpError as exc:
            raise WorkerRepositoryClientError(str(exc)) from exc
        raw = response.text
        if response.status_code < 200 or response.status_code >= 300:
            raise WorkerRepositoryClientError(
                raw or f"worker repository returned HTTP {response.status_code}"
            )
        if not raw:
            return {}
        try:
            return _JSON_OBJECT.validate_json(raw)
        except ValidationError:
            raise WorkerRepositoryClientError("invalid worker repository JSON response") from None

    def stream(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> Iterator[JsonObject]:
        try:
            with self.http.stream(
                "POST",
                self._url(path),
                headers=self._headers(),
                content=_encoded(payload),
                timeout_seconds=self.timeout_seconds,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raw = response.read().decode("utf-8")
                    raise WorkerRepositoryClientError(
                        raw or f"worker repository returned HTTP {response.status_code}"
                    )
                yield from _iter_sse_data(response.iter_lines())
        except InternalHttpError as exc:
            raise WorkerRepositoryClientError(str(exc)) from exc

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _url(self, path: str) -> str:
        endpoint = urlparse(self.endpoint)
        if endpoint.scheme not in {"http", "https"} or endpoint.hostname is None:
            msg = "worker repository endpoint must be an HTTP(S) URL with a hostname"
            raise WorkerRepositoryClientError(msg)
        base = self.endpoint.rstrip("/")
        return f"{base}/{path.lstrip('/')}"


class WorkerRepositoryTransport(Protocol):
    def post(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> JsonObject: ...

    def stream(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> Iterator[JsonObject]: ...

    def set_bearer_token(self, token: str) -> None: ...


@runtime_checkable
class WorkerRepositoryShutdownTransport(Protocol):
    def prepare_shutdown(self, *, timeout_seconds: float) -> None: ...


@dataclass(slots=True)
class WorkerRepositoryHttpClient:
    transport: WorkerRepositoryTransport

    def prepare_shutdown(self, *, timeout_seconds: float) -> None:
        if isinstance(self.transport, WorkerRepositoryShutdownTransport):
            self.transport.prepare_shutdown(timeout_seconds=timeout_seconds)

    def get_next_container_request(
        self,
        request: GetNextContainerRequestRequest,
    ) -> GetNextContainerRequestResponse:
        last_response = GetNextContainerRequestResponse()
        for response in self.stream_next_container_requests(request):
            last_response = response
            if response.container_request is not None:
                return response
        return last_response

    def stream_next_container_requests(
        self,
        request: GetNextContainerRequestRequest,
    ) -> Iterator[GetNextContainerRequestResponse]:
        yield from self._stream_model(
            "/worker-repository/get-next-container-request",
            request,
            GetNextContainerRequestResponse,
        )

    def stream_worker_events(
        self,
        request: StreamWorkerEventsRequest,
    ) -> Iterator[WorkerStreamEvent]:
        yield from self._stream_model(
            "/worker-repository/stream-worker-events",
            request,
            WorkerStreamEvent,
        )

    def acknowledge_container_request(
        self,
        request: AcknowledgeContainerRequestRequest,
    ) -> AcknowledgeContainerRequestResponse:
        return self._post_model(
            "/worker-repository/acknowledge-container-request",
            request,
            AcknowledgeContainerRequestResponse,
        )

    def report_image_build_progress(
        self, request: ReportImageBuildProgressRequest
    ) -> ReportImageBuildProgressResponse:
        return self._post_model(
            "/worker-repository/report-image-build-progress",
            request,
            ReportImageBuildProgressResponse,
        )

    def report_image_build_result(
        self,
        request: ReportImageBuildResultRequest,
    ) -> ReportImageBuildResultResponse:
        return self._post_model(
            "/worker-repository/report-image-build-result",
            request,
            ReportImageBuildResultResponse,
        )

    def acknowledge_worker_event(self, event_id: str, worker_id: str) -> None:
        response = self._post_model(
            "/worker-repository/acknowledge-worker-event",
            AcknowledgeWorkerEventRequest(event_id=event_id, worker_id=worker_id),
            AcknowledgeWorkerEventResponse,
        )
        if not response.acknowledged:
            raise WorkerRepositoryClientError(
                f"worker event {event_id!r} was not acknowledged for {worker_id!r}"
            )

    def get_worker_by_id(self, request: WorkerIdRequest) -> GetWorkerByIdResponse:
        return self._post_model(
            "/worker-repository/get-worker-by-id",
            request,
            GetWorkerByIdResponse,
        )

    def add_worker(self, request: AddWorkerRequest) -> WorkerRecordResponse:
        response = self._post_model(
            "/worker-repository/add-worker",
            request,
            WorkerRecordResponse,
        )
        if response.worker_session_token:
            self.transport.set_bearer_token(response.worker_session_token)
        return response

    def activate_source_cache(
        self,
        request: WorkerCacheSessionRequest,
    ) -> WorkerRecordResponse:
        return self._post_model(
            "/worker-repository/toggle-worker-available",
            request,
            WorkerRecordResponse,
        )

    def disable_worker(self, request: DisableWorkerRequest) -> WorkerRecordResponse:
        return self._post_model(
            "/worker-repository/disable-worker",
            request,
            WorkerRecordResponse,
        )

    def remove_worker(self, request: WorkerIdRequest) -> RemoveWorkerResponse:
        return self._post_model(
            "/worker-repository/remove-worker",
            request,
            RemoveWorkerResponse,
        )

    def update_worker_capacity(
        self,
        request: UpdateWorkerCapacityRequest,
    ) -> UpdateWorkerCapacityResponse:
        return self._post_model(
            "/worker-repository/update-worker-capacity",
            request,
            UpdateWorkerCapacityResponse,
        )

    def set_worker_keep_alive(
        self,
        request: WorkerCacheSessionRequest,
    ) -> WorkerKeepAliveResponse:
        return self._post_model(
            "/worker-repository/set-worker-keep-alive",
            request,
            WorkerKeepAliveResponse,
        )

    def claim_source_cache_cleanup(
        self,
        request: ClaimSourceCacheCleanupRequest,
    ) -> ClaimSourceCacheCleanupResponse:
        return self._post_model(
            "/worker-repository/claim-source-cache-cleanup",
            request,
            ClaimSourceCacheCleanupResponse,
        )

    def complete_source_cache_cleanup(
        self,
        request: ResolveSourceCacheCleanupRequest,
    ) -> ResolveSourceCacheCleanupResponse:
        return self._post_model(
            "/worker-repository/complete-source-cache-cleanup",
            request,
            ResolveSourceCacheCleanupResponse,
        )

    def fail_source_cache_cleanup(
        self,
        request: ResolveSourceCacheCleanupRequest,
    ) -> ResolveSourceCacheCleanupResponse:
        return self._post_model(
            "/worker-repository/fail-source-cache-cleanup",
            request,
            ResolveSourceCacheCleanupResponse,
        )

    def update_container_status(
        self,
        request: UpdateContainerStatusRequest,
    ) -> UpdateContainerStatusResponse:
        return self._post_model(
            "/worker-repository/update-container-status",
            request,
            UpdateContainerStatusResponse,
        )

    def set_container_exit_code(
        self,
        request: SetContainerExitCodeRequest,
    ) -> SetContainerExitCodeResponse:
        return self._post_model(
            "/worker-repository/set-container-exit-code",
            request,
            SetContainerExitCodeResponse,
        )

    def get_container_state(
        self,
        request: GetContainerStateRequest,
    ) -> GetContainerStateResponse:
        return self._post_model(
            "/worker-repository/get-container-state",
            request,
            GetContainerStateResponse,
        )

    def delete_container_state(
        self,
        request: DeleteContainerStateRequest,
    ) -> DeleteContainerStateResponse:
        return self._post_model(
            "/worker-repository/delete-container-state",
            request,
            DeleteContainerStateResponse,
        )

    def set_worker_address(
        self,
        request: SetWorkerAddressRequest,
    ) -> SetWorkerAddressResponse:
        return self._post_model(
            "/worker-repository/set-worker-address",
            request,
            SetWorkerAddressResponse,
        )

    def set_container_address(
        self,
        request: SetContainerAddressRequest,
    ) -> SetContainerAddressResponse:
        return self._post_model(
            "/worker-repository/set-container-address",
            request,
            SetContainerAddressResponse,
        )

    def set_container_address_map(
        self,
        request: SetContainerAddressMapRequest,
    ) -> SetContainerAddressMapResponse:
        return self._post_model(
            "/worker-repository/set-container-address-map",
            request,
            SetContainerAddressMapResponse,
        )

    def get_container_address(
        self,
        request: GetContainerAddressRequest,
    ) -> GetContainerAddressResponse:
        return self._post_model(
            "/worker-repository/get-container-address",
            request,
            GetContainerAddressResponse,
        )

    def get_worker_address(
        self,
        request: GetWorkerAddressRequest,
    ) -> GetWorkerAddressResponse:
        return self._post_model(
            "/worker-repository/get-worker-address",
            request,
            GetWorkerAddressResponse,
        )

    def get_container_address_map(
        self,
        request: GetContainerAddressMapRequest,
    ) -> GetContainerAddressMapResponse:
        return self._post_model(
            "/worker-repository/get-container-address-map",
            request,
            GetContainerAddressMapResponse,
        )

    def get_cache_origin_credentials(
        self,
        request: CacheOriginCredentialRequest,
    ) -> GetCacheOriginCredentialsResponse:
        return self._post_model(
            "/worker-repository/get-cache-origin-credentials",
            request,
            GetCacheOriginCredentialsResponse,
        )

    def get_image_archive_upload_credentials(
        self,
        request: ImageArchiveUploadCredentialRequest,
    ) -> GetImageArchiveUploadCredentialsResponse:
        return self._post_model(
            "/worker-repository/get-image-archive-upload-credentials",
            request,
            GetImageArchiveUploadCredentialsResponse,
        )

    def get_image_build_credentials(
        self,
        request: GetImageBuildCredentialsRequest,
    ) -> GetImageBuildCredentialsResponse:
        return self._post_model(
            "/worker-repository/get-image-build-credentials",
            request,
            GetImageBuildCredentialsResponse,
        )

    def prepare_image_build_context_download(
        self,
        request: PrepareImageBuildContextDownloadRequest,
    ) -> PrepareImageBuildContextDownloadResponse:
        return self._post_model(
            "/worker-repository/prepare-image-build-context-download",
            request,
            PrepareImageBuildContextDownloadResponse,
        )

    def get_container_credentials(
        self,
        request: ContainerCredentialRequest,
    ) -> GetContainerCredentialsResponse:
        return self._post_model(
            "/worker-repository/get-container-credentials",
            request,
            GetContainerCredentialsResponse,
        )

    def set_network_lock(self, request: NetworkLockRequest) -> NetworkLockResponse:
        return self._post_model(
            "/worker-repository/set-network-lock",
            request,
            NetworkLockResponse,
        )

    def remove_network_lock(
        self,
        request: RemoveNetworkLockRequest,
    ) -> RemoveNetworkLockResponse:
        return self._post_model(
            "/worker-repository/remove-network-lock",
            request,
            RemoveNetworkLockResponse,
        )

    def set_container_ip(self, request: SetContainerIpRequest) -> SetContainerIpResponse:
        return self._post_model(
            "/worker-repository/set-container-ip",
            request,
            SetContainerIpResponse,
        )

    def move_container_ip(self, request: MoveContainerIpRequest) -> MoveContainerIpResponse:
        return self._post_model(
            "/worker-repository/move-container-ip",
            request,
            MoveContainerIpResponse,
        )

    def get_container_ip_assignments(
        self,
        request: GetContainerIpAssignmentsRequest,
    ) -> GetContainerIpAssignmentsResponse:
        return self._post_model(
            "/worker-repository/get-container-ip-assignments",
            request,
            GetContainerIpAssignmentsResponse,
        )

    def remove_container_ip(
        self,
        request: RemoveContainerIpRequest,
    ) -> RemoveContainerIpResponse:
        return self._post_model(
            "/worker-repository/remove-container-ip",
            request,
            RemoveContainerIpResponse,
        )

    def save_checkpoint_state(
        self,
        request: SaveCheckpointStateRequest,
    ) -> SaveCheckpointStateResponse:
        return self._post_model(
            "/worker-repository/save-checkpoint-state",
            request,
            SaveCheckpointStateResponse,
        )

    def acquire_automatic_checkpoint_lease(
        self,
        request: AcquireAutomaticCheckpointLeaseRequest,
    ) -> AcquireAutomaticCheckpointLeaseResponse:
        return self._post_model(
            "/worker-repository/acquire-automatic-checkpoint-lease",
            request,
            AcquireAutomaticCheckpointLeaseResponse,
        )

    def release_automatic_checkpoint_lease(
        self,
        request: ReleaseAutomaticCheckpointLeaseRequest,
    ) -> ReleaseAutomaticCheckpointLeaseResponse:
        return self._post_model(
            "/worker-repository/release-automatic-checkpoint-lease",
            request,
            ReleaseAutomaticCheckpointLeaseResponse,
        )

    def get_checkpoint_restore(
        self,
        request: GetCheckpointRestoreRequest,
    ) -> GetCheckpointRestoreResponse:
        return self._post_model(
            "/worker-repository/get-checkpoint-restore",
            request,
            GetCheckpointRestoreResponse,
        )

    def persist_checkpoint_archive(
        self,
        request: PersistCheckpointArchiveRequest,
    ) -> PersistCheckpointArchiveResponse:
        return self._post_model(
            "/worker-repository/persist-checkpoint-archive",
            request,
            PersistCheckpointArchiveResponse,
        )

    def prepare_checkpoint_archive_upload(
        self,
        request: PrepareCheckpointArchiveUploadRequest,
    ) -> PrepareCheckpointArchiveUploadResponse:
        return self._post_model(
            "/worker-repository/prepare-checkpoint-archive-upload",
            request,
            PrepareCheckpointArchiveUploadResponse,
        )

    def publish_worker_event(
        self,
        request: PublishWorkerEventRequest,
    ) -> PublishWorkerEventResponse:
        return self._post_model(
            "/worker-repository/publish-worker-event",
            request,
            PublishWorkerEventResponse,
        )

    def record_worker_usage(
        self,
        request: RecordWorkerUsageRequest,
    ) -> RecordWorkerUsageResponse:
        return self._post_model(
            "/worker-repository/record-worker-usage",
            request,
            RecordWorkerUsageResponse,
        )

    def publish_container_lifecycle(
        self,
        request: PublishContainerLifecycleRequest,
    ) -> PublishContainerLifecycleResponse:
        return self._post_model(
            "/worker-repository/publish-container-lifecycle",
            request,
            PublishContainerLifecycleResponse,
        )

    def publish_container_metrics(
        self,
        request: PublishContainerMetricsRequest,
    ) -> PublishContainerMetricsResponse:
        return self._post_model(
            "/worker-repository/publish-container-metrics",
            request,
            PublishContainerMetricsResponse,
        )

    def publish_container_event(
        self,
        request: PublishContainerEventRequest,
    ) -> PublishContainerEventResponse:
        return self._post_model(
            "/worker-repository/publish-container-event",
            request,
            PublishContainerEventResponse,
        )

    def append_sandbox_process_log(
        self,
        request: AppendSandboxProcessLogRequest,
    ) -> AppendSandboxProcessLogResponse:
        return self._post_model(
            "/worker-repository/append-sandbox-process-log",
            request,
            AppendSandboxProcessLogResponse,
        )

    def append_container_logs(
        self,
        request: AppendContainerLogsRequest,
    ) -> AppendContainerLogsResponse:
        return self._post_model(
            "/worker-repository/append-container-logs",
            request,
            AppendContainerLogsResponse,
        )

    def _post_model[T: ContractModel](
        self,
        path: str,
        request: ContractModel,
        response_type: type[T],
    ) -> T:
        try:
            return response_type.model_validate(self.transport.post(path, _model_payload(request)))
        except ValidationError:
            raise WorkerRepositoryClientError(
                f"worker repository returned an invalid response for {path}"
            ) from None

    def _stream_model[T: ContractModel](
        self,
        path: str,
        request: ContractModel,
        response_type: type[T],
    ) -> Iterator[T]:
        for payload in self.transport.stream(path, _model_payload(request)):
            try:
                yield response_type.model_validate(payload)
            except ValidationError:
                raise WorkerRepositoryClientError(
                    f"worker repository returned an invalid stream response for {path}"
                ) from None


@dataclass(slots=True)
class RemoteWorkerRepositoryState:
    worker_id: str
    container_states: dict[str, SchedulerContainerState] = field(default_factory=dict)
    deleted_container_ids: set[str] = field(default_factory=set)
    cache_session: WorkerCacheSession | None = None

    def remember_request(self, request: SchedulerWorkerRequest) -> None:
        self.deleted_container_ids.discard(request.container_id)
        self.container_states.setdefault(
            request.container_id,
            SchedulerContainerState(
                container_id=request.container_id,
                stub_id=request.stub_id,
                workspace_id=request.workspace_id,
                worker_id=self.worker_id,
                cpu_millicores=request.cpu_millicores,
                memory_mib=request.memory_mib,
                gpu_type=next(iter(request.gpu), ""),
                gpu_count=request.gpu_count,
                status=SchedulerContainerStatus.Pending,
                scheduled_at=request.timestamp,
            ),
        )

    def update_container_state(self, state: SchedulerContainerState) -> SchedulerContainerState:
        self.deleted_container_ids.discard(state.container_id)
        self.container_states[state.container_id] = state
        return state


@dataclass(slots=True)
class RemoteSchedulerWorkerRepository:
    client: WorkerRepositoryHttpClient
    state: RemoteWorkerRepositoryState
    source_cache_identity: WorkerSourceCacheIdentity
    source_cache_materializer: SourceCodePackageMaterializer
    _source_cache_reconciler: WorkerSourceCacheReconciler = field(init=False)
    _available_worker: SchedulerWorkerRecord | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._source_cache_reconciler = WorkerSourceCacheReconciler(
            repository=self,
            materializer=self.source_cache_materializer,
        )

    def prepare_shutdown(self, *, timeout_seconds: float) -> None:
        self.client.prepare_shutdown(timeout_seconds=timeout_seconds)

    def get_next_container_request(self, worker_id: str) -> SchedulerWorkerRequest | None:
        session = self._cache_session()
        response = self.client.get_next_container_request(
            GetNextContainerRequestRequest(
                worker_id=worker_id,
                cache_generation_id=session.generation_id,
                cache_session_fence=session.session_fence,
            )
        )
        request = response.container_request
        if request is not None:
            self.state.remember_request(request)
        return request

    def acknowledge_worker_request(self, worker_id: str, container_id: str) -> bool:
        return self.client.acknowledge_container_request(
            AcknowledgeContainerRequestRequest(
                worker_id=worker_id,
                container_id=container_id,
            )
        ).acknowledged

    def report_image_build_result(
        self,
        request: SchedulerWorkerRequest,
        result: WorkerImageBuildExecutionResult,
    ) -> None:
        response = self.client.report_image_build_result(
            ReportImageBuildResultRequest(
                worker_id=self.state.worker_id,
                workspace_id=request.workspace_id,
                container_id=request.container_id,
                build_id=result.build_id,
                image_id=result.image_id,
                status=BuildStatus.Complete if result.ok else BuildStatus.Failed,
                object_key=result.object_key,
                archive_size_bytes=result.archive_size_bytes,
                archive_sha256=result.archive_sha256,
                logs=[_bounded_image_build_log(line) for line in result.logs[-256:]],
                error_message=result.error_message[:65_536],
            )
        )
        if not response.accepted:
            raise WorkerRepositoryClientError(
                f"image build result was not accepted for {result.build_id!r}"
            )

    def report_image_build_progress(
        self, request: SchedulerWorkerRequest, *, after: int, logs: list[str]
    ) -> int:
        return self.client.report_image_build_progress(
            ReportImageBuildProgressRequest(
                worker_id=self.state.worker_id,
                workspace_id=request.workspace_id,
                container_id=request.container_id,
                build_id=request.container_id,
                after=after,
                logs=[_bounded_image_build_log(line) for line in logs],
            )
        ).sequence

    def get_worker(self, worker_id: str) -> SchedulerWorkerRecord | None:
        return self.client.get_worker_by_id(WorkerIdRequest(worker_id=worker_id)).worker

    def add_worker(
        self,
        worker: SchedulerWorkerRecord,
        *,
        ttl_seconds: int = 0,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord:
        _ = now
        response = self.client.add_worker(
            AddWorkerRequest(
                worker=worker,
                cache_generation_id=self.source_cache_identity.generation_id,
                cache_storage_id=self.source_cache_identity.storage_id,
                ttl_seconds=ttl_seconds,
            )
        )
        if response.worker is None:
            msg = f"worker {worker.worker_id!r} was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        if response.cache_session is None:
            raise WorkerRepositoryClientError(
                f"worker {worker.worker_id!r} source cache session was not returned"
            )
        record_source_cache_session(
            self.source_cache_materializer.cache_root,
            self.source_cache_identity,
            session_fence=response.cache_session.session_fence,
        )
        self.state.cache_session = response.cache_session
        self._available_worker = None
        return response.worker

    def prepare_source_cache(self) -> None:
        """Drive one cleanup round before this worker serves.

        A target names an object whose store bytes and row are already gone,
        so nothing can request what remains cached here. A failed purge is
        retried from the durable queue while the worker serves; only a round
        that cannot run at all — an unreachable control plane or a lost cache
        session — refuses registration.
        """
        self.reconcile_source_cache()

    def toggle_worker_available(
        self,
        worker_id: str,
        *,
        ttl_seconds: int = 0,
    ) -> SchedulerWorkerRecord:
        _ = ttl_seconds
        worker = self._available_worker
        if worker is None:
            msg = f"worker {worker_id!r} was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return worker

    def set_keep_alive(
        self,
        worker_id: str,
        *,
        ttl_seconds: int = 0,
    ) -> SchedulerWorkerRecord:
        _ = ttl_seconds
        response = self.client.set_worker_keep_alive(self._session_request())
        if response.source_cache_state in {
            WorkerCacheGenerationState.Initializing,
            WorkerCacheGenerationState.Draining,
        }:
            # Each keepalive drives another cleanup round; a purge that still
            # fails stays queued and the worker keeps serving.
            self.reconcile_source_cache()
            response = self.client.set_worker_keep_alive(self._session_request())
        worker = response.worker
        if worker is None:
            if response.source_cache_state is not WorkerCacheGenerationState.Available:
                raise WorkerSourceCacheNotAvailableError(
                    worker_id,
                    response.source_cache_state,
                )
            msg = f"worker {worker_id!r} was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return worker

    def reconcile_source_cache(self) -> WorkerSourceCacheReconcileResult:
        return self._source_cache_reconciler.reconcile()

    def claim_source_cache_cleanup(self, *, limit: int) -> WorkerSourceCacheClaimSource:
        response = self.client.claim_source_cache_cleanup(
            ClaimSourceCacheCleanupRequest(
                **self._session_request().model_dump(),
                limit=limit,
            )
        )
        return WorkerSourceCacheClaimSource(targets=response.targets)

    def complete_source_cache_cleanup(self, target: SourceCacheCleanupTargetRecord) -> None:
        claim_token = target.claim_token
        if not claim_token:
            raise WorkerRepositoryClientError(
                f"source cache cleanup target {target.id!r} has no claim token"
            )
        response = self.client.complete_source_cache_cleanup(
            ResolveSourceCacheCleanupRequest(
                **self._session_request().model_dump(),
                target_id=target.id,
                claim_token=claim_token,
            )
        )
        if not response.resolved:
            raise WorkerRepositoryClientError(
                f"source cache cleanup target {target.id!r} was not completed"
            )

    def fail_source_cache_cleanup(self, target: SourceCacheCleanupTargetRecord) -> None:
        claim_token = target.claim_token
        if not claim_token:
            raise WorkerRepositoryClientError(
                f"source cache cleanup target {target.id!r} has no claim token"
            )
        response = self.client.fail_source_cache_cleanup(
            ResolveSourceCacheCleanupRequest(
                **self._session_request().model_dump(),
                target_id=target.id,
                claim_token=claim_token,
            )
        )
        if not response.resolved:
            raise WorkerRepositoryClientError(
                f"source cache cleanup target {target.id!r} failure was not recorded"
            )

    def activate_source_cache(self) -> None:
        response = self.client.activate_source_cache(self._session_request())
        if response.worker is None:
            raise WorkerRepositoryClientError("activated worker was not returned by repository")
        self._available_worker = response.worker

    def _session_request(self) -> WorkerCacheSessionRequest:
        session = self._cache_session()
        return WorkerCacheSessionRequest(
            worker_id=self.state.worker_id,
            cache_generation_id=session.generation_id,
            cache_session_fence=session.session_fence,
        )

    def _cache_session(self) -> WorkerCacheSession:
        if self.state.cache_session is None:
            raise WorkerRepositoryClientError("worker source cache session is not registered")
        return self.state.cache_session

    def reconcile_worker_capacity(self, worker_id: str) -> SchedulerWorkerRecord:
        return self.set_keep_alive(worker_id)

    def disable_worker(
        self,
        worker_id: str,
        *,
        reason: WorkerUnavailableReason,
        detail: str = "",
        ttl_seconds: int = 0,
    ) -> SchedulerWorkerRecord:
        _ = ttl_seconds
        worker = self.client.disable_worker(
            DisableWorkerRequest(worker_id=worker_id, reason=reason, detail=detail)
        ).worker
        if worker is None:
            msg = f"worker {worker_id!r} was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return worker

    def remove_worker(self, worker_id: str) -> WorkerRemovalResult:
        removal = self.client.remove_worker(WorkerIdRequest(worker_id=worker_id)).removal
        if removal is None:
            msg = f"worker {worker_id!r} removal was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return removal

    def update_worker_capacity(
        self,
        worker_id: str,
        request: SchedulerWorkerRequest,
        change: WorkerCapacityChange,
    ) -> WorkerCapacityPlan:
        response = self.client.update_worker_capacity(
            UpdateWorkerCapacityRequest(
                worker_id=worker_id,
                container_request=request,
                change=change,
            )
        )
        if response.plan is None:
            msg = f"worker {worker_id!r} capacity plan was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.plan


@dataclass(slots=True)
class RemoteSchedulerContainerRepository:
    client: WorkerRepositoryHttpClient
    state: RemoteWorkerRepositoryState

    def get_container_state(self, container_id: str) -> SchedulerContainerState | None:
        response = self.client.get_container_state(
            GetContainerStateRequest(container_id=container_id)
        )
        if response.state is None:
            self.state.container_states.pop(container_id, None)
            return None
        return self.state.update_container_state(response.state)

    def update_container_status(
        self,
        container_id: str,
        status: SchedulerContainerStatus,
        *,
        ttl_seconds: int,
    ) -> ContainerStatusUpdatePlan:
        response = self.client.update_container_status(
            UpdateContainerStatusRequest(
                container_id=container_id,
                status=status,
                ttl_seconds=ttl_seconds,
            )
        )
        if response.state is None or response.plan is None:
            msg = f"container {container_id!r} status update was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        self.state.update_container_state(response.state)
        return response.plan

    def set_exit_code(
        self,
        container_id: str,
        exit_code: int,
        *,
        termination_reason: StopContainerReason = StopContainerReason.Unknown,
        failed_phase: ContainerExecutionPhase | None = None,
        failure_detail: str = "",
    ) -> None:
        self.client.set_container_exit_code(
            SetContainerExitCodeRequest(
                container_id=container_id,
                exit_code=exit_code,
                termination_reason=termination_reason,
                failed_phase=failed_phase,
                failure_detail=failure_detail,
            )
        )

    def delete_container_state(self, container_id: str) -> bool:
        response = self.client.delete_container_state(
            DeleteContainerStateRequest(container_id=container_id)
        )
        self.state.deleted_container_ids.add(container_id)
        self.state.container_states.pop(container_id, None)
        return response.deleted

    def set_worker_address(
        self,
        container_id: str,
        address: str,
        *,
        route: AgentBackendRoute | None = None,
    ) -> SchedulerContainerAddress:
        response = self.client.set_worker_address(
            SetWorkerAddressRequest(container_id=container_id, address=address, route=route)
        )
        if response.address is None:
            msg = f"worker address for container {container_id!r} was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.address

    def set_container_address(
        self,
        container_id: str,
        address: str,
        *,
        route: AgentBackendRoute | None = None,
    ) -> SchedulerContainerAddress:
        response = self.client.set_container_address(
            SetContainerAddressRequest(container_id=container_id, address=address, route=route)
        )
        if response.address is None:
            msg = f"container address for {container_id!r} was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.address

    def set_container_address_map(
        self,
        container_id: str,
        address_map: dict[int, str],
        *,
        routes: list[AgentBackendRoute] | None = None,
    ) -> SchedulerContainerAddressMap:
        response = self.client.set_container_address_map(
            SetContainerAddressMapRequest(
                container_id=container_id,
                address_map=address_map,
                routes=routes or [],
            )
        )
        if response.address_map is None:
            msg = f"container address map for {container_id!r} was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.address_map

    def get_container_address(self, container_id: str) -> SchedulerContainerAddress | None:
        return self.client.get_container_address(
            GetContainerAddressRequest(container_id=container_id)
        ).address

    def get_worker_address(self, container_id: str) -> SchedulerContainerAddress | None:
        return self.client.get_worker_address(
            GetWorkerAddressRequest(container_id=container_id)
        ).address

    def get_container_address_map(self, container_id: str) -> SchedulerContainerAddressMap:
        address_map = self.client.get_container_address_map(
            GetContainerAddressMapRequest(container_id=container_id)
        ).address_map
        return address_map or SchedulerContainerAddressMap(container_id=container_id)


@dataclass(slots=True)
class RemoteWorkerNetworkIpRepository:
    client: WorkerRepositoryHttpClient

    def set_network_lock(
        self,
        network_prefix: str,
        *,
        ttl_seconds: int = 30,
        retries: int = 0,
    ) -> WorkerRepositoryLockRecord:
        response = self.client.set_network_lock(
            NetworkLockRequest(
                ttl_seconds=ttl_seconds,
                retries=retries,
            )
        )
        if response.lock is None:
            msg = f"network {network_prefix!r} lock was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.lock

    def remove_network_lock(
        self,
        network_prefix: str,
        token: str,
    ) -> WorkerRepositoryLockRelease:
        response = self.client.remove_network_lock(RemoveNetworkLockRequest(token=token))
        if response.release is None:
            msg = f"network {network_prefix!r} lock release was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.release

    def list_assignments(self, network_prefix: str) -> list[ContainerIpAssignment]:
        return list(
            self.client.get_container_ip_assignments(GetContainerIpAssignmentsRequest()).assignments
        )

    def set_container_ip(
        self,
        network_prefix: str,
        container_id: str,
        ip_address: str,
    ) -> NetworkIpMutationPlan:
        response = self.client.set_container_ip(
            SetContainerIpRequest(
                container_id=container_id,
                ip_address=ip_address,
            )
        )
        if response.plan is None:
            msg = f"container {container_id!r} IP assignment was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.plan

    def move_container_ip(
        self,
        network_prefix: str,
        from_container_id: str,
        to_container_id: str,
        ip_address: str,
    ) -> NetworkIpMutationPlan:
        response = self.client.move_container_ip(
            MoveContainerIpRequest(
                from_container_id=from_container_id,
                to_container_id=to_container_id,
                ip_address=ip_address,
            )
        )
        if response.plan is None:
            msg = f"container {to_container_id!r} IP move was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.plan

    def remove_container_ip(
        self,
        network_prefix: str,
        container_id: str,
    ) -> NetworkIpMutationPlan:
        response = self.client.remove_container_ip(
            RemoveContainerIpRequest(container_id=container_id)
        )
        if response.plan is None:
            msg = f"container {container_id!r} IP release was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.plan


@dataclass(slots=True)
class RemoteWorkerCredentialService:
    client: WorkerRepositoryHttpClient

    def vend(
        self,
        request: ContainerCredentialRequest,
        *,
        principal: WorkerCredentialPrincipal,
    ) -> ContainerCredentials:
        _ = principal
        response = self.client.get_container_credentials(request)
        if response.credentials is None:
            msg = "container credentials were not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.credentials


@dataclass(slots=True)
class RemoteCheckpointStateSink:
    client: WorkerRepositoryHttpClient

    def save_checkpoint_state(self, payload: CheckpointStatePayload) -> CheckpointRecord:
        response = self.client.save_checkpoint_state(SaveCheckpointStateRequest(payload=payload))
        if response.checkpoint is None:
            msg = f"checkpoint {payload.checkpoint_id!r} was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.checkpoint


@dataclass(slots=True)
class RemoteAutomaticCheckpointCreationLeaseCoordinator:
    client: WorkerRepositoryHttpClient

    def acquire(
        self,
        *,
        workspace_id: str,
        stub_id: str,
        owner_token: str,
        ttl_seconds: int,
    ) -> AutomaticCheckpointCreationLease:
        response = self.client.acquire_automatic_checkpoint_lease(
            AcquireAutomaticCheckpointLeaseRequest(
                workspace_id=workspace_id,
                stub_id=stub_id,
                container_id=owner_token,
                ttl_seconds=ttl_seconds,
            )
        )
        return AutomaticCheckpointCreationLease(
            acquired=response.acquired,
            available_checkpoint_id=response.available_checkpoint_id,
        )

    def release(
        self,
        *,
        workspace_id: str,
        stub_id: str,
        owner_token: str,
    ) -> bool:
        return self.client.release_automatic_checkpoint_lease(
            ReleaseAutomaticCheckpointLeaseRequest(
                workspace_id=workspace_id,
                stub_id=stub_id,
                container_id=owner_token,
            )
        ).released


@dataclass(slots=True)
class RemoteContainerLogSink:
    client: WorkerRepositoryHttpClient

    def publish_container_logs(
        self,
        *,
        container_id: str,
        capture_id: str,
        entries: list[ContainerLogBatchEntry],
    ) -> AppendContainerLogsResponse:
        return self.client.append_container_logs(
            AppendContainerLogsRequest(
                container_id=container_id,
                capture_id=capture_id,
                entries=entries,
            )
        )


@dataclass(slots=True)
class RemoteWorkerEventSink:
    client: WorkerRepositoryHttpClient

    def append(self, record: WorkerEventRecord) -> WorkerEventRecord:
        response = self.client.publish_worker_event(PublishWorkerEventRequest(record=record))
        if response.record is None:
            msg = f"worker event {record.id!r} was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.record


@dataclass(slots=True)
class RemoteWorkerUsageRecorder:
    client: WorkerRepositoryHttpClient

    def record(
        self,
        *,
        id: str | None = None,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        metric: UsageMetric,
        quantity: float,
        unit: UsageUnit,
        labels: dict[str, str] | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> UsageRecord:
        record = UsageRecord(
            id=id or str(uuid4()),
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            metric=metric,
            quantity=quantity,
            unit=unit,
            labels=labels or {},
            metadata=metadata or {},
        )
        response = self.client.record_worker_usage(RecordWorkerUsageRequest(record=record))
        if response.record is None:
            msg = f"usage record {record.id!r} was not returned by repository"
            raise WorkerRepositoryClientError(msg)
        return response.record


@dataclass(slots=True)
class RemoteContainerLifecycleSink:
    client: WorkerRepositoryHttpClient
    worker_id: str = ""

    def publish_container_lifecycle(self, payload: ContainerLifecyclePayload) -> CloudEventRecord:
        published = (
            payload.model_copy(update={"worker_id": self.worker_id})
            if self.worker_id and not payload.worker_id
            else payload
        )
        response = self.client.publish_container_lifecycle(
            PublishContainerLifecycleRequest(payload=published)
        )
        if response.event is None:
            msg = f"container lifecycle event for {payload.container_id!r} was not returned"
            raise WorkerRepositoryClientError(msg)
        return response.event


@dataclass(slots=True)
class RemoteContainerMetricsSink:
    client: WorkerRepositoryHttpClient

    def publish_container_metrics(self, payload: ContainerMetricsPayload) -> CloudEventRecord:
        response = self.client.publish_container_metrics(
            PublishContainerMetricsRequest(payload=payload)
        )
        if response.event is None:
            msg = f"container metrics event for {payload.container_id!r} was not returned"
            raise WorkerRepositoryClientError(msg)
        return response.event


@dataclass(slots=True)
class RemoteSandboxProcessLogSink:
    client: WorkerRepositoryHttpClient

    def append_sandbox_process_log(self, entry: SandboxProcessLogEntry) -> None:
        response = self.client.append_sandbox_process_log(
            AppendSandboxProcessLogRequest(entry=entry)
        )
        if response.event is None:
            msg = f"sandbox process log for container {entry.container_id!r} was not returned"
            raise WorkerRepositoryClientError(msg)


def build_worker_repository_http_client(
    *,
    endpoint: str,
    token: str,
    timeout_seconds: float = 30.0,
    http: InternalHttpClient | None = None,
) -> WorkerRepositoryHttpClient:
    if not endpoint:
        msg = "worker repository endpoint is required"
        raise WorkerRepositoryClientError(msg)
    if not token:
        msg = "worker repository token is required"
        raise WorkerRepositoryClientError(msg)
    return WorkerRepositoryHttpClient(
        WorkerRepositoryHttpTransport(
            endpoint=endpoint,
            token=token,
            timeout_seconds=timeout_seconds,
            http=http or InternalHttpClient(timeout_seconds=timeout_seconds),
        )
    )


def _model_payload(model: ContractModel) -> JsonObject:
    return _JSON_OBJECT.validate_json(model.model_dump_json())


def _encoded(payload: Mapping[str, JsonValue]) -> bytes:
    return json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")


def _iter_sse_data(lines: Iterator[str]) -> Iterator[JsonObject]:
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            if data_lines:
                yield _parse_sse_data("\n".join(data_lines))
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    if data_lines:
        yield _parse_sse_data("\n".join(data_lines))


def _parse_sse_data(data: str) -> JsonObject:
    try:
        return _JSON_OBJECT.validate_json(data)
    except ValidationError as exc:
        raise WorkerRepositoryClientError(f"invalid worker repository event data: {data}") from exc
