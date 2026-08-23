from __future__ import annotations

import hashlib
import logging
import tempfile
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from compute.state import RedisComputeStateRepository
from control.deployment_resources import DeploymentResourceService
from coordination.event_bus import (
    EventBusEvent,
    EventBusEventType,
    RedisEventBus,
    event_channel_key,
)
from coordination.redis_client import RedisClient, redis_text
from database.context import ServiceContext
from database.repositories.compute import ComputeUnitRepository
from database.repositories.execution import TaskRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from database.types import DatabaseSession
from execution.containers.preemption import PreemptedContainerControl
from execution.containers.runtime_state import ContainerRuntimeStateRepository
from execution.tasks import TaskService
from foundation.network import worker_network_prefix
from gateway.unit_state import billing_owner_for_unit
from identity.auth import AuthorizationDeniedError, AuthService
from images.service import ImageBuildService
from observability.container_logs import (
    ContainerLogIngestionService,
    ContainerLogRuntimeAttribution,
)
from observability.stream_state import RedisEventStreamRepository
from observability.usage import UsageService, WorkerEventService
from observability.workspace_changes import WorkspaceChangeService
from pydantic import JsonValue
from scheduler.state import (
    ContainerStateNotFoundError,
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerNetworkIpRepository,
    SchedulerRepositoryError,
)
from shared.app_identity import NAME
from shared.cache_records import CacheEntry
from shared.compute_policy import ComputeUnitRecord
from shared.container_requests import StopContainerReason
from shared.containers import TERMINAL_CONTAINER_STATUSES, ContainerRecord, ContainerStatus
from shared.errors import (
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.identity import AuthScope, TokenStatus
from shared.image_building.records import BuildStatus
from shared.objects import ObjectRecord
from shared.realtime.contracts import EventRecordType
from shared.routing import AgentBackendRoute
from shared.scheduling import (
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    SchedulerWorkerStatus,
    WorkerUnavailableReason,
    worker_serves_owner,
)
from shared.source_cache_cleanup import WorkerCacheGenerationState
from shared.tasks import Task, TaskStatus, is_terminal_task_status
from shared.timestamps import to_utc, utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageBillingOwner,
    UsageRecord,
)
from storage.service import CacheStorage, ObjectStorage
from worker.event_bridge import worker_stream_event_from_bus_event
from worker.events import (
    WORKER_EVENT_HEARTBEAT_ID,
    ContainerExecutionPhase,
    ContainerLifecyclePayload,
    WorkerStreamEvent,
    WorkerStreamEventKind,
)
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
    GetContainerIpRequest,
    GetContainerIpResponse,
    GetContainerIpsRequest,
    GetContainerIpsResponse,
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
    RemoveImagePullLockRequest,
    RemoveImagePullLockResponse,
    RemoveNetworkLockRequest,
    RemoveNetworkLockResponse,
    RemoveWorkerResponse,
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
    SetImagePullLockRequest,
    SetImagePullLockResponse,
    SetWorkerAddressRequest,
    SetWorkerAddressResponse,
    StreamWorkerEventsRequest,
    UpdateContainerStatusRequest,
    UpdateContainerStatusResponse,
    UpdateWorkerCapacityRequest,
    UpdateWorkerCapacityResponse,
    WorkerCacheSession,
    WorkerCacheSessionRequest,
    WorkerContainerIndexRequest,
    WorkerContainerIndexResponse,
    WorkerIdRequest,
    WorkerKeepAliveResponse,
    WorkerRecordResponse,
    WorkerRepositoryPrincipal,
)
from worker.tools import ContainerCredentialRequest
from worker_repository.admission import (
    WorkerRequestNotAdmissibleError,
    require_admissible_worker_request,
)
from worker_repository.checkpoint_records import (
    AutomaticCheckpointCreationLeaseService,
    CheckpointService,
)
from worker_repository.credentials import (
    WorkerCredentialService,
)
from worker_repository.image_build_credentials import (
    RedisImageBuildCredentialCache,
    RedisImageBuildUploadCapabilityGuard,
)
from worker_repository.origin_credentials import WorkerCacheOriginCredentialService
from worker_repository.source_cache import (
    WorkerSourceCacheService,
    WorkerSourceCacheUnavailableError,
)

LOGGER = logging.getLogger(__name__)
WORKER_REQUEST_BLOCK_SECONDS = 1.0
IMAGE_BUILD_CONTEXT_DOWNLOAD_SECONDS = 300
_CONTAINER_RESOURCE = "container"
"""What a worker names as the subject of the usage it reports. It is the only
resource a worker is dispatched, so it is the only one it may bill for."""

_METERING_WINDOW_TOLERANCE = timedelta(minutes=1)
"""How far past a container's recorded lifetime a worker's own window may reach.

The worker times itself from the moment it launches the process, while the
control plane stamps `started_at` when it is told the container is running and
`finished_at` when it is told it exited; neither clock is synchronized with the
other. A tolerance narrower than that lag would clamp away seconds a customer
really used, and a much wider one is what a worker would claim to be paid for
hours nobody ran."""


class WorkerRepositoryObjectStorage(Protocol):
    def put_file(self, bucket: str, key: str, source: str | Path) -> ObjectRecord: ...

    def get(self, bucket: str, key: str) -> ObjectRecord: ...

    def get_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
    ) -> ObjectRecord: ...

    def get_by_id_for_workspace(
        self,
        object_id: str,
        *,
        workspace_id: str,
    ) -> ObjectRecord: ...

    def reserve(
        self,
        bucket: str,
        key: str,
        *,
        size: int,
        sha256: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> ObjectRecord: ...

    def reserve_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        size: int,
        sha256: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> ObjectRecord: ...

    def download_file(self, bucket: str, key: str, target: str | Path) -> ObjectRecord: ...

    def download_file_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        target: str | Path,
    ) -> ObjectRecord: ...

    def generate_presigned_get_url_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        expires_seconds: int = 3600,
    ) -> str: ...

    def generate_presigned_put_url_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        expires_seconds: int,
        content_length: int,
        content_type: str,
    ) -> str: ...


class WorkerRepositoryCacheStorage(Protocol):
    def put(self, namespace: str, key: str, source: str | Path) -> CacheEntry: ...


@dataclass(frozen=True, slots=True)
class WorkerRepositoryDependencies:
    context: ServiceContext
    auth: AuthService
    deployment_resources: DeploymentResourceService
    checkpoints: CheckpointService
    images: ImageBuildService
    object_storage: WorkerRepositoryObjectStorage
    worker_events: WorkerEventService
    usage: UsageService
    workspace_changes: WorkspaceChangeService
    preempted_containers: PreemptedContainerControl
    tasks: TaskService


FATAL_CONTAINER_STARTUP_PHASES = frozenset(
    {
        ContainerExecutionPhase.PublishWorkerAddress.value,
        ContainerExecutionPhase.HydrateCredentials.value,
        ContainerExecutionPhase.LoadImage.value,
        ContainerExecutionPhase.AllocatePorts.value,
        ContainerExecutionPhase.PublishContainerRoutes.value,
        ContainerExecutionPhase.SetupNetwork.value,
        ContainerExecutionPhase.SetupMounts.value,
        ContainerExecutionPhase.AssignGpu.value,
        ContainerExecutionPhase.BuildSpec.value,
        ContainerExecutionPhase.PrepareRuntime.value,
        ContainerExecutionPhase.RunRuntime.value,
    }
)


def _scheduler_domain_error(exc: SchedulerRepositoryError) -> DomainError:
    if isinstance(exc, ContainerStateNotFoundError):
        return NotFoundError(str(exc))
    return ConflictError(str(exc))


def _require_worker_principal(
    principal: WorkerRepositoryPrincipal,
    *,
    operation: str,
) -> None:
    if not (principal.is_managed_worker or principal.is_private_worker):
        raise AuthorizationDeniedError(f"{operation} requires a worker principal")


@dataclass(frozen=True, slots=True)
class AgentRouteReconciliationResult:
    scanned: int = 0
    removed: int = 0


@dataclass(slots=True)
class WorkerRepositoryService:
    workers: RedisSchedulerWorkerRepository
    containers: RedisSchedulerContainerRepository
    network: RedisWorkerNetworkIpRepository
    events: RedisEventBus
    container_credentials: WorkerCredentialService
    origin_credentials: WorkerCacheOriginCredentialService
    dependencies: WorkerRepositoryDependencies | None = None
    redis: RedisClient | None = None
    runtime_state: ContainerRuntimeStateRepository | None = None
    object_storage: WorkerRepositoryObjectStorage | None = None
    cache_storage: WorkerRepositoryCacheStorage | None = None
    source_cache: WorkerSourceCacheService | None = None

    @property
    def services(self) -> WorkerRepositoryDependencies | None:
        return self.dependencies

    def _authorize_worker_tenancy(
        self,
        principal: WorkerRepositoryPrincipal,
        workspace_id: str,
        *,
        operation: str,
    ) -> None:
        """Refuse an operation for a workspace this worker's account does not own.

        A private worker is one customer's machine and serves every workspace that
        customer owns, so the comparison is by account. Comparing the worker's own
        enrolling workspace refused its owner's other workspaces, which is exactly
        the capacity they connected the hardware for.

        The worker's account is read from its scheduler record, which the platform
        stamps from the machine's enrollment; the workspace's account is read from
        membership. Neither is anything the worker reports about itself.
        """
        _require_worker_principal(principal, operation=operation)
        if not principal.is_private_worker:
            return
        worker = self.workers.get_worker(principal.worker_id) if principal.worker_id else None
        if worker is None:
            raise AuthorizationDeniedError(f"{operation} worker is no longer registered")
        if not worker_serves_owner(
            private_worker=True,
            worker_owner_user_id=worker.owner_user_id,
            request_owner_user_id=self._workspace_owner_user_id(workspace_id),
        ):
            raise AuthorizationDeniedError(
                f"{operation} workspace does not belong to the worker's account"
            )

    def _workspace_owner_user_id(self, workspace_id: str) -> str:
        if self.services is None:
            raise UpstreamUnavailableError(
                "service dependencies are required to resolve workspace ownership"
            )
        with self.services.context.database.session() as session:
            owner = WorkspaceMemberRepository(session).owner(workspace_id)
        # A workspace with no owner answers empty, which no private worker matches.
        return owner.user_id if owner is not None else ""

    def _validate_worker_stream(
        self,
        worker_id: str,
        *,
        principal: WorkerRepositoryPrincipal | None,
    ) -> SchedulerWorkerRecord:
        worker = self.workers.get_worker(worker_id)
        if worker is None:
            raise UpstreamUnavailableError(f"assigned worker state is unavailable: {worker_id}")
        if principal is not None and principal.worker_id != worker_id:
            raise ConflictError("worker request does not match the authenticated worker")
        private_principal = (
            principal if principal is not None and principal.is_private_worker else None
        )
        if private_principal is not None:
            if not worker.private_worker:
                raise ConflictError("private worker is not marked for private compute")
            if not worker.requires_pool_selector:
                raise ConflictError("private worker must require an explicit pool selector")
            if not worker.machine_id:
                raise UpstreamUnavailableError(
                    f"private worker machine identity is unavailable: {worker_id}"
                )
            if self.services is None:
                raise UpstreamUnavailableError(
                    "service dependencies are required for private worker admission"
                )
            with self.services.context.database.session() as session:
                durable_worker = WorkerRepository(session).get(
                    worker_id,
                    workspace_id=private_principal.workspace_id,
                )
                if durable_worker is None or durable_worker.pool != worker.pool:
                    raise ConflictError(
                        f"worker {worker_id} enrollment does not match scheduler state"
                    )
        return worker

    def _record_worker_queue_lifecycle(
        self,
        request: SchedulerWorkerRequest,
        *,
        worker_id: str,
    ) -> None:
        events = self._event_streams()
        if events is None:
            return
        received_at = utc_now()
        duration_ms = max(int((received_at - request.timestamp).total_seconds() * 1000), 0)
        try:
            events.append_event(
                EventRecordType.ContainerLifecycle,
                {
                    "id": "worker.queue",
                    "event_id": "worker.queue",
                    "container_id": request.container_id,
                    "stub_id": request.stub_id,
                    "workspace_id": request.workspace_id,
                    "worker_id": worker_id,
                    "start_time": request.timestamp,
                    "end_time": received_at,
                    "duration_ms": duration_ms,
                    "success": True,
                    "attrs": {"phase": "worker.queue"},
                },
            )
        except Exception:
            LOGGER.warning(
                "worker queue lifecycle publication failed",
                exc_info=True,
                extra={"container_id": request.container_id, "worker_id": worker_id},
            )

    def _return_request_to_scheduler(
        self,
        request: SchedulerWorkerRequest,
        *,
        worker_id: str,
        reason: str,
    ) -> None:
        """Give a request this worker can never run back to the scheduler.

        The worker queue is the one place it must not go: this worker already refused
        it, and the claim on the ready queue was acked when it was dispatched, so the
        scheduler is the only party that can place it elsewhere or fail it with a
        reason. The retry count is what bounds that—a request refused repeatedly
        reaches the retry limit and fails naming the mismatch, rather than circulating
        forever.
        """

        self.workers.return_worker_request(
            worker_id,
            request.model_copy(update={"retry_count": request.retry_count + 1}),
            ready_at=utc_now(),
        )
        LOGGER.warning(
            "worker refused a container request it can never run; returned to the scheduler",
            extra={
                "worker_id": worker_id,
                "container_id": request.container_id,
                "reason": reason,
            },
        )

    def stream_next_container_requests(
        self,
        request: GetNextContainerRequestRequest,
        *,
        principal: WorkerRepositoryPrincipal | None = None,
    ) -> Iterator[GetNextContainerRequestResponse]:
        while True:
            worker = self._validate_worker_stream(request.worker_id, principal=principal)
            try:
                self._require_source_cache_available(request, principal=principal)
            except WorkerSourceCacheUnavailableError:
                # A cache generation that is still initializing is an expected
                # transient, not a server fault. Raising here escapes as an
                # unhandled ASGI error because the streaming response has already
                # started, so it can never be mapped and instead buries real
                # failures under repeated tracebacks. Ending the stream lets the
                # worker poll again once its generation is available.
                return
            container_request = self.workers.wait_for_next_container_request(
                request.worker_id,
                timeout_seconds=WORKER_REQUEST_BLOCK_SECONDS,
            )
            if container_request is None:
                yield GetNextContainerRequestResponse()
                continue
            try:
                self._require_source_cache_available(request, principal=principal)
            except Exception:
                self.workers.enqueue_worker_request(request.worker_id, container_request)
                raise
            try:
                require_admissible_worker_request(
                    worker,
                    container_request,
                    principal=principal,
                    # Only the private-worker branch reads it, so the shared fleet
                    # does not pay a membership query per dispatched container.
                    request_owner_user_id=(
                        self._workspace_owner_user_id(container_request.workspace_id)
                        if principal is not None and principal.is_private_worker
                        else ""
                    ),
                )
            except WorkerRequestNotAdmissibleError as exc:
                self._return_request_to_scheduler(
                    container_request,
                    worker_id=request.worker_id,
                    reason=str(exc),
                )
                # Ending the stream rather than raising, for the reason recorded above:
                # the response has already started, so a raise escapes unmapped.
                return
            self._record_worker_queue_lifecycle(container_request, worker_id=request.worker_id)
            yield GetNextContainerRequestResponse(container_request=container_request)
            # One request per stream. It is in flight until the worker
            # acknowledges it, and the take returns an unacknowledged request
            # ahead of the queue, so continuing here would hand the same one back
            # in a loop instead of waiting for the worker to resolve it.
            return

    def acknowledge_container_request(
        self,
        request: AcknowledgeContainerRequestRequest,
    ) -> AcknowledgeContainerRequestResponse:
        """Record that the worker has taken the container this request names.

        Delivery is at least once up to this call and nothing after it: the
        request is redeliverable until the worker says it holds the container, and
        the durable row's start deadline covers a worker that says so and then
        dies before starting.
        """

        return AcknowledgeContainerRequestResponse(
            acknowledged=self.workers.acknowledge_worker_request(
                request.worker_id,
                request.container_id,
            )
        )

    def stream_worker_events(
        self,
        request: StreamWorkerEventsRequest,
    ) -> Iterator[WorkerStreamEvent]:
        emitted = 0
        for event_id in self._pending_worker_event_ids(request.worker_id):
            if not self._event_targets_worker(event_id, request.worker_id):
                if self.redis is not None:
                    self.redis.set_remove(self._pending_event_key(request.worker_id), event_id)
                continue
            event = self._worker_event_from_id(event_id)
            if event is None:
                if self.redis is not None:
                    self.redis.set_remove(self._pending_event_key(request.worker_id), event_id)
                continue
            yield event
            emitted += 1
            if request.max_events > 0 and emitted >= request.max_events:
                return
        for event_id in request.event_ids:
            if not self._event_targets_worker(event_id, request.worker_id):
                continue
            event = self._worker_event_from_id(event_id)
            if event is None:
                continue
            yield event
            emitted += 1
            if request.max_events > 0 and emitted >= request.max_events:
                return

        if request.max_events > 0 and emitted >= request.max_events:
            return

        yielded_pubsub = False
        for event_id in self._pubsub_worker_event_ids(request):
            yielded_pubsub = True
            event = self._worker_event_from_id(event_id)
            if event is None:
                continue
            yield event
            emitted += 1
            if request.max_events > 0 and emitted >= request.max_events:
                return

        if not yielded_pubsub and (request.max_events <= 0 or emitted < request.max_events):
            yield _heartbeat_event()

    def acknowledge_worker_event(
        self,
        request: AcknowledgeWorkerEventRequest,
    ) -> AcknowledgeWorkerEventResponse:
        if self.redis is None or self.workers.get_worker(request.worker_id) is None:
            return AcknowledgeWorkerEventResponse(acknowledged=False)
        self.redis.set_add(self._event_ack_key(request.event_id), request.worker_id)
        self.redis.expire(self._event_ack_key(request.event_id), 300)
        self.redis.set_remove(self._pending_event_key(request.worker_id), request.event_id)
        return AcknowledgeWorkerEventResponse(acknowledged=True)

    def wake_source_cache_cleanup(self, workspace_id: str) -> None:
        del workspace_id
        if self.redis is None:
            return
        event = EventBusEvent(type=EventBusEventType.PurgeSourceCache)
        self.events.send(event)

    def set_image_pull_lock(
        self,
        request: SetImagePullLockRequest,
    ) -> SetImagePullLockResponse:
        return SetImagePullLockResponse(
            lock=self.workers.set_image_pull_lock(
                request.worker_id,
                request.image_id,
                ttl_seconds=request.ttl_seconds,
                retries=request.retries,
            )
        )

    def remove_image_pull_lock(
        self,
        request: RemoveImagePullLockRequest,
    ) -> RemoveImagePullLockResponse:
        return RemoveImagePullLockResponse(
            release=self.workers.remove_image_pull_lock(
                request.worker_id,
                request.image_id,
                request.token,
            )
        )

    def add_container_to_worker(
        self,
        request: WorkerContainerIndexRequest,
    ) -> WorkerContainerIndexResponse:
        return WorkerContainerIndexResponse(
            count=self.workers.add_container_to_worker(request.worker_id, request.container_id)
        )

    def remove_container_from_worker(
        self,
        request: WorkerContainerIndexRequest,
    ) -> WorkerContainerIndexResponse:
        return WorkerContainerIndexResponse(
            count=self.workers.remove_container_from_worker(
                request.worker_id,
                request.container_id,
            )
        )

    def get_worker_by_id(self, request: WorkerIdRequest) -> GetWorkerByIdResponse:
        try:
            return GetWorkerByIdResponse(worker=self.workers.get_worker(request.worker_id))
        except SchedulerRepositoryError as exc:
            raise _scheduler_domain_error(exc) from exc

    def add_worker(
        self,
        request: AddWorkerRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> WorkerRecordResponse:
        self._validate_runtime_worker_registration(request.worker, principal)
        source_cache = self._source_cache_service()
        generation = source_cache.register(
            principal=principal,
            worker_id=request.worker.worker_id,
            generation_id=request.cache_generation_id,
            storage_id=request.cache_storage_id,
        )
        initializing_worker = request.worker.model_copy(
            update={
                "status": SchedulerWorkerStatus.Pending,
                # The token decides which tenant a worker serves. Taking the
                # registration's own value would let a worker name any workspace and
                # be scheduled that workspace's work.
                "workspace_id": principal.workspace_id,
                # The switch the owner comparison hangs on, so it answers to the
                # token like the others. Left to the registration, a machine its
                # customer holds root on can call itself public, and placement
                # then skips the owner check entirely and offers it every
                # account's work — the default pool carries one name for every
                # workspace, so it does not even have to guess which.
                "private_worker": principal.is_private_worker,
                # Stamped with it, because placement compares accounts: a private
                # worker whose owner is unset serves nobody, so registering without
                # one refuses the machine every request until a reconcile repairs it.
                "owner_user_id": (
                    self._workspace_owner_user_id(principal.workspace_id)
                    if principal.is_private_worker
                    else ""
                ),
                # Same authority again, and the one with money behind it: a worker
                # runs on hardware its owner may hold root on, so a registration
                # naming its own billing owner could mark every container it runs
                # self-hosted and drop them from the bill.
                "billing_owner": self._billing_owner_for(request.worker, principal),
                # Whose pool this is decides who may land on it, so the unit
                # answers rather than the machine. A worker is launched by an
                # agent holding a config that cannot see the unit, so left to the
                # registration every worker declares itself selector-only and a
                # pool that serves general work has none that will take it.
                "requires_pool_selector": not self._feeding_unit(
                    request.worker, principal
                ).default_eligible,
            }
        )
        try:
            if request.ttl_seconds > 0:
                worker = self.workers.add_worker(
                    initializing_worker,
                    ttl_seconds=request.ttl_seconds,
                )
            else:
                worker = self.workers.add_worker(initializing_worker)
            try:
                self._sync_runtime_worker_registration(worker, principal)
            except Exception:
                with suppress(SchedulerRepositoryError):
                    self.workers.remove_worker(worker.worker_id)
                raise
            return WorkerRecordResponse(
                worker=worker,
                worker_session_token=self._worker_session_token(
                    principal,
                    worker_id=worker.worker_id,
                ),
                cache_session=WorkerCacheSession(
                    generation_id=generation.id,
                    session_fence=generation.session_fence,
                ),
            )
        except SchedulerRepositoryError as exc:
            raise _scheduler_domain_error(exc) from exc

    def _billing_owner_for(
        self, worker: SchedulerWorkerRecord, principal: WorkerRepositoryPrincipal
    ) -> UsageBillingOwner:
        """Who pays for containers this worker runs.

        The fleet is the platform's own capacity and is neither joined nor
        enrolled, so it never has a unit to ask. A private worker takes the owner
        of the unit that feeds its pool — a unit holding a provider connection is
        a customer's own cloud account and earns the management fee, anything else
        is hardware somebody brought.
        """

        if not principal.is_private_worker:
            return UsageBillingOwner.PlatformFleet
        return billing_owner_for_unit(self._feeding_unit(worker, principal))

    def _feeding_unit(
        self, worker: SchedulerWorkerRecord, principal: WorkerRepositoryPrincipal
    ) -> ComputeUnitRecord:
        """The unit a registering worker belongs to.

        A pool is fed by any number of units, so the worker is admitted on the
        unit it names and the pool is checked against that unit. The two failures
        stay distinct: a pool no unit feeds yet may still be provisioning and is
        worth retrying, while an owner that does not feed the pool it claims can
        never succeed.
        """
        if self.services is None:
            raise UpstreamUnavailableError(
                "service dependencies are required for worker registration"
            )
        with self.services.context.database.session() as session:
            feeding = ComputeUnitRepository(session).list_for_machine_pool(
                principal.workspace_id, worker.pool
            )
        if not feeding:
            raise UpstreamUnavailableError(f"worker capacity pool is unavailable: {worker.pool}")
        for unit in feeding:
            if unit.capacity_owner_id == worker.capacity_owner_id:
                return unit
        raise ConflictError(f"worker capacity owner does not match pool {worker.pool}")

    def _validate_runtime_worker_registration(
        self,
        worker: SchedulerWorkerRecord,
        principal: WorkerRepositoryPrincipal,
    ) -> None:
        if self.services is None:
            raise UpstreamUnavailableError(
                "service dependencies are required for worker registration"
            )
        if not worker.capacity_owner_id:
            raise ConflictError("worker registration requires a capacity owner identity")
        self._feeding_unit(worker, principal)
        if not principal.is_private_worker:
            return
        if not worker.machine_id:
            raise ConflictError("private worker requires a machine identity")
        with self.services.context.database.session() as session:
            workers = WorkerRepository(session)
            durable_worker = workers.get_across_workspaces(worker.worker_id)
            machine = MachineRepository(session).get_across_workspaces(worker.machine_id)
            if durable_worker is None or machine is None:
                raise UpstreamUnavailableError(
                    f"remote worker enrollment is unavailable: {worker.worker_id}"
                )
            if (
                workers.workspace_id(worker.worker_id) != principal.workspace_id
                or MachineRepository(session).workspace_id(worker.machine_id)
                != principal.workspace_id
            ):
                raise ConflictError(
                    f"worker {worker.worker_id} does not belong to registration workspace"
                )
            if durable_worker.machine_id != worker.machine_id or durable_worker.pool != worker.pool:
                raise ConflictError(
                    f"worker {worker.worker_id} enrollment does not match registration"
                )

    def _sync_runtime_worker_registration(
        self,
        worker: SchedulerWorkerRecord,
        principal: WorkerRepositoryPrincipal,
    ) -> None:
        if not principal.is_private_worker or self.services is None:
            return
        if not worker.machine_id:
            raise ConflictError("private worker requires a machine identity")
        now = utc_now()
        with self.services.context.database.session() as session:
            workers = WorkerRepository(session)
            durable_worker = workers.get_across_workspaces(worker.worker_id)
            if durable_worker is None:
                raise UpstreamUnavailableError(
                    f"remote worker enrollment is unavailable: {worker.worker_id}"
                )
            workers.upsert(
                # Registration is inventory, not readiness. Whether this worker
                # takes work is the scheduler record's answer, and asserting it
                # here claimed it before the worker had validated anything.
                durable_worker.model_copy(
                    update={
                        "machine_id": worker.machine_id,
                        "pool": worker.pool,
                        "last_seen_at": now,
                    }
                ),
                workspace_id=principal.workspace_id,
            )
        self.services.workspace_changes.emit_change(
            workspace_id=principal.workspace_id,
            topic=WorkspaceChangeTopic.ComputeWorkers,
            change=WorkspaceChangeType.Updated,
            resource_id=worker.worker_id,
        )

    def _worker_session_token(
        self,
        principal: WorkerRepositoryPrincipal,
        *,
        worker_id: str,
    ) -> str:
        if principal.worker_id:
            return ""
        if self.services is None:
            raise UpstreamUnavailableError(
                "service dependencies are required for worker session credentials"
            )
        auth = self.services.auth
        for token in auth.list_workspace_tokens(principal.workspace_id):
            if token.worker_id == worker_id and token.status is TokenStatus.Active:
                auth.revoke_token(token.id)
        raw_token, _record = auth.create_token(
            f"worker-session-{worker_id}-{uuid4().hex[:8]}",
            scopes=[AuthScope.Worker.value],
            kind=principal.token_kind,
            workspace_id=principal.workspace_id,
            worker_id=worker_id,
        )
        return raw_token

    def revoke_worker_session(self, principal: WorkerRepositoryPrincipal) -> None:
        if self.services is None:
            raise UpstreamUnavailableError(
                "service dependencies are required for worker session credentials"
            )
        self.services.auth.revoke_token(principal.token_id)

    def toggle_worker_available(
        self,
        request: WorkerCacheSessionRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> WorkerRecordResponse:
        self._source_cache_service().activate(
            principal=principal,
            worker_id=request.worker_id,
            generation_id=request.cache_generation_id,
            session_fence=request.cache_session_fence,
        )
        try:
            current = self.workers.get_worker(request.worker_id)
            if current is not None and current.status is SchedulerWorkerStatus.Available:
                return WorkerRecordResponse(worker=current)
            return WorkerRecordResponse(
                worker=self.workers.toggle_worker_available(request.worker_id)
            )
        except SchedulerRepositoryError as exc:
            raise _scheduler_domain_error(exc) from exc

    def disable_worker(self, request: DisableWorkerRequest) -> WorkerRecordResponse:
        try:
            return WorkerRecordResponse(
                worker=self.workers.disable_worker(
                    request.worker_id,
                    reason=request.reason,
                    detail=request.detail,
                )
            )
        except SchedulerRepositoryError as exc:
            raise _scheduler_domain_error(exc) from exc

    def remove_worker(self, request: WorkerIdRequest) -> RemoveWorkerResponse:
        try:
            return RemoveWorkerResponse(removal=self.workers.remove_worker(request.worker_id))
        except SchedulerRepositoryError as exc:
            raise _scheduler_domain_error(exc) from exc

    def update_worker_capacity(
        self,
        request: UpdateWorkerCapacityRequest,
    ) -> UpdateWorkerCapacityResponse:
        try:
            return UpdateWorkerCapacityResponse(
                plan=self.workers.update_worker_capacity(
                    request.worker_id,
                    request.container_request,
                    request.change,
                )
            )
        except SchedulerRepositoryError as exc:
            raise _scheduler_domain_error(exc) from exc

    def set_worker_keep_alive(
        self,
        request: WorkerCacheSessionRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> WorkerKeepAliveResponse:
        generation = self._source_cache_service().current(
            principal=principal,
            worker_id=request.worker_id,
            generation_id=request.cache_generation_id,
            session_fence=request.cache_session_fence,
        )
        if generation.state not in {
            WorkerCacheGenerationState.Available,
            WorkerCacheGenerationState.Draining,
        }:
            self._project_source_cache_unavailable(request.worker_id)
            return WorkerKeepAliveResponse(source_cache_state=generation.state)
        try:
            return WorkerKeepAliveResponse(
                worker=self.workers.set_keep_alive(request.worker_id),
                source_cache_state=generation.state,
            )
        except SchedulerRepositoryError as exc:
            raise _scheduler_domain_error(exc) from exc

    def claim_source_cache_cleanup(
        self,
        request: ClaimSourceCacheCleanupRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> ClaimSourceCacheCleanupResponse:
        claim = self._source_cache_service().claim(
            principal=principal,
            worker_id=request.worker_id,
            generation_id=request.cache_generation_id,
            session_fence=request.cache_session_fence,
            limit=request.limit,
        )
        if claim.generation_state is WorkerCacheGenerationState.Initializing:
            self._project_source_cache_unavailable(request.worker_id)
        return ClaimSourceCacheCleanupResponse(targets=claim.targets)

    def complete_source_cache_cleanup(
        self,
        request: ResolveSourceCacheCleanupRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> ResolveSourceCacheCleanupResponse:
        self._source_cache_service().complete(
            principal=principal,
            worker_id=request.worker_id,
            generation_id=request.cache_generation_id,
            session_fence=request.cache_session_fence,
            target_id=request.target_id,
            claim_token=request.claim_token,
        )
        return ResolveSourceCacheCleanupResponse(resolved=True)

    def fail_source_cache_cleanup(
        self,
        request: ResolveSourceCacheCleanupRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> ResolveSourceCacheCleanupResponse:
        self._source_cache_service().fail(
            principal=principal,
            worker_id=request.worker_id,
            generation_id=request.cache_generation_id,
            session_fence=request.cache_session_fence,
            target_id=request.target_id,
            claim_token=request.claim_token,
        )
        return ResolveSourceCacheCleanupResponse(resolved=True)

    def _require_source_cache_available(
        self,
        request: WorkerCacheSessionRequest | GetNextContainerRequestRequest,
        *,
        principal: WorkerRepositoryPrincipal | None,
    ) -> None:
        if principal is None:
            raise AuthorizationDeniedError("worker source cache session requires a principal")
        try:
            self._source_cache_service().require_available(
                principal=principal,
                worker_id=request.worker_id,
                generation_id=request.cache_generation_id,
                session_fence=request.cache_session_fence,
            )
        except WorkerSourceCacheUnavailableError:
            self._project_source_cache_unavailable(request.worker_id)
            raise

    def _project_source_cache_unavailable(self, worker_id: str) -> None:
        try:
            worker = self.workers.get_worker(worker_id)
            if worker is not None and worker.status in {
                SchedulerWorkerStatus.Available,
                SchedulerWorkerStatus.Pending,
            }:
                self.workers.disable_worker(
                    worker_id,
                    reason=WorkerUnavailableReason.SourceCacheUnavailable,
                )
        except SchedulerRepositoryError:
            return

    def _source_cache_service(self) -> WorkerSourceCacheService:
        if self.source_cache is None:
            raise UpstreamUnavailableError("worker source cache persistence is unavailable")
        return self.source_cache

    def update_container_status(
        self,
        request: UpdateContainerStatusRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> UpdateContainerStatusResponse:
        self._authorize_worker_container(
            request.container_id,
            worker_id=principal.worker_id,
            operation="container status update",
        )
        try:
            plan = self.containers.update_container_status(
                request.container_id,
                request.status,
                ttl_seconds=request.ttl_seconds,
            )
        except SchedulerRepositoryError as exc:
            raise _scheduler_domain_error(exc) from exc
        self._sync_runtime_container_status(request.container_id, plan.next_status)
        state = self.containers.get_container_state(request.container_id)
        if state is None:
            raise NotFoundError(f"container state not found: {request.container_id}")
        if plan.release_concurrency and state.worker_id:
            with suppress(SchedulerRepositoryError):
                self.workers.reconcile_worker_capacity(state.worker_id)
        return UpdateContainerStatusResponse(state=state, plan=plan)

    def set_container_exit_code(
        self,
        request: SetContainerExitCodeRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> SetContainerExitCodeResponse:
        self._authorize_worker_container(
            request.container_id,
            worker_id=principal.worker_id,
            operation="container exit",
        )
        self.containers.set_exit_code(
            request.container_id,
            request.exit_code,
            termination_reason=request.termination_reason,
            ttl_seconds=request.ttl_seconds,
        )
        self._sync_runtime_container_exit(
            request.container_id,
            request.exit_code,
            failed_phase=request.failed_phase,
            failure_detail=request.failure_detail,
            termination_reason=request.termination_reason,
        )
        return SetContainerExitCodeResponse(container_id=request.container_id)

    def get_container_state(
        self,
        request: GetContainerStateRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> GetContainerStateResponse:
        state = self.containers.get_container_state(request.container_id)
        # A state that is not there discloses nothing, and a worker reads it to
        # find out whether the request it was handed is still live — refusing
        # that would turn a container cancelled before it started into an error
        # on the worker that was told to drop it.
        if state is not None and state.worker_id != principal.worker_id:
            raise AuthorizationDeniedError(
                "container state read names a container assigned to another worker"
            )
        return GetContainerStateResponse(state=state)

    def delete_container_state(
        self,
        request: DeleteContainerStateRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> DeleteContainerStateResponse:
        self._authorize_worker_container(
            request.container_id,
            worker_id=principal.worker_id,
            operation="container state deletion",
        )
        routes = self._container_agent_routes(request.container_id)
        self._unpublish_agent_routes(routes)
        return DeleteContainerStateResponse(
            deleted=self.containers.delete_container_state(request.container_id)
        )

    def reconcile_orphan_agent_routes(self) -> AgentRouteReconciliationResult:
        if self.redis is None:
            return AgentRouteReconciliationResult()
        compute = RedisComputeStateRepository(self.redis)
        routes = compute.scan_agent_route_states()
        active_routes: dict[str, set[str]] = {}
        removed = 0
        for route in routes:
            route_ids = active_routes.get(route.container_id)
            if route_ids is None:
                state = self.containers.get_container_state(route.container_id)
                route_ids = (
                    self._container_agent_route_ids(route.container_id)
                    if state is not None
                    and state.status
                    not in {
                        SchedulerContainerStatus.Complete,
                        SchedulerContainerStatus.Failed,
                        SchedulerContainerStatus.Stopping,
                    }
                    else set[str]()
                )
                active_routes[route.container_id] = route_ids
            if route.route_id in route_ids:
                continue
            # The stored record carries the owner it was saved under, so delete the
            # key that was actually written. Passing the pool here made the orphan
            # sweep a no-op: the delete missed, the index kept the route, and the
            # agent went on dialing dead backends every stream iteration.
            if compute.delete_agent_route_state(
                workspace_id=route.workspace_id,
                capacity_owner_id=route.capacity_owner_id,
                machine_id=route.machine_id,
                route_id=route.route_id,
            ):
                removed += 1
        return AgentRouteReconciliationResult(scanned=len(routes), removed=removed)

    def set_worker_address(
        self,
        request: SetWorkerAddressRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> SetWorkerAddressResponse:
        self._authorize_worker_container(
            request.container_id,
            worker_id=principal.worker_id,
            operation="worker address publication",
        )
        address = self.containers.set_worker_address(
            request.container_id,
            request.address,
            route=request.route,
        )
        self._publish_agent_routes([address.route] if address.route is not None else [])
        return SetWorkerAddressResponse(address=address)

    def set_container_address(
        self,
        request: SetContainerAddressRequest,
    ) -> SetContainerAddressResponse:
        address = self.containers.set_container_address(
            request.container_id,
            request.address,
            route=request.route,
        )
        self._publish_agent_routes([address.route] if address.route is not None else [])
        return SetContainerAddressResponse(address=address)

    def set_container_address_map(
        self,
        request: SetContainerAddressMapRequest,
    ) -> SetContainerAddressMapResponse:
        address_map = self.containers.set_container_address_map(
            request.container_id,
            request.address_map,
            routes=request.routes,
        )
        self._publish_agent_routes(address_map.routes)
        return SetContainerAddressMapResponse(address_map=address_map)

    def get_container_address(
        self,
        request: GetContainerAddressRequest,
    ) -> GetContainerAddressResponse:
        return GetContainerAddressResponse(
            address=self.containers.get_container_address(request.container_id)
        )

    def get_worker_address(self, request: GetWorkerAddressRequest) -> GetWorkerAddressResponse:
        return GetWorkerAddressResponse(
            address=self.containers.get_worker_address(request.container_id)
        )

    def _publish_agent_routes(self, routes: list[AgentBackendRoute]) -> None:
        if self.redis is None:
            return
        repository = RedisComputeStateRepository(self.redis)
        for route in routes:
            if not (route.route_id and route.workspace_id and route.pool and route.machine_id):
                continue
            # Keyed by the machine, from its worker record, rather than by the
            # container's workspace: a backend route is a port on one host, and the
            # agent reads its routes under the workspace and unit that enrolled it.
            # Keying by the container's workspace filed a route the owning agent
            # could never see, so a workload from another of the account's
            # workspaces waited for a route nothing would ever open.
            worker = self.workers.get_worker(route.worker_id)
            if worker is None or not (worker.capacity_owner_id and worker.workspace_id):
                continue
            repository.save_agent_route_state(
                route.model_copy(
                    update={
                        "workspace_id": worker.workspace_id,
                        "capacity_owner_id": worker.capacity_owner_id,
                    }
                )
            )

    def _unpublish_agent_routes(self, routes: list[AgentBackendRoute]) -> None:
        if self.redis is None:
            return
        repository = RedisComputeStateRepository(self.redis)
        # Deleted under the same owner key the publish path wrote, resolved the
        # same way, or the route outlives its container.
        unique_routes: set[tuple[str, str, str, str]] = set()
        for route in routes:
            if not (route.route_id and route.workspace_id and route.pool and route.machine_id):
                continue
            worker = self.workers.get_worker(route.worker_id)
            if worker is None or not (worker.capacity_owner_id and worker.workspace_id):
                continue
            unique_routes.add(
                (worker.workspace_id, worker.capacity_owner_id, route.machine_id, route.route_id)
            )
        for workspace_id, capacity_owner_id, machine_id, route_id in sorted(unique_routes):
            repository.delete_agent_route_state(
                workspace_id=workspace_id,
                capacity_owner_id=capacity_owner_id,
                machine_id=machine_id,
                route_id=route_id,
            )

    def _container_agent_routes(self, container_id: str) -> list[AgentBackendRoute]:
        routes = list(self.containers.get_container_address_map(container_id).routes)
        address = self.containers.get_container_address(container_id)
        if address is not None and address.route is not None:
            routes.append(address.route)
        worker_address = self.containers.get_worker_address(container_id)
        if worker_address is not None and worker_address.route is not None:
            routes.append(worker_address.route)
        return routes

    def _container_agent_route_ids(self, container_id: str) -> set[str]:
        return {
            route.route_id for route in self._container_agent_routes(container_id) if route.route_id
        }

    def get_container_address_map(
        self,
        request: GetContainerAddressMapRequest,
    ) -> GetContainerAddressMapResponse:
        return GetContainerAddressMapResponse(
            address_map=self.containers.get_container_address_map(request.container_id)
        )

    def get_cache_origin_credentials(
        self,
        request: CacheOriginCredentialRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> GetCacheOriginCredentialsResponse:
        if not principal.worker_id:
            raise AuthorizationDeniedError("image archive download requires a worker principal")
        self._authorize_worker_tenancy(
            principal,
            request.workspace_id,
            operation="image archive download",
        )
        state = self.containers.get_container_state(request.container_id)
        if state is None:
            raise AuthorizationDeniedError("image archive download container is not assigned")
        if state.status not in {
            SchedulerContainerStatus.Pending,
            SchedulerContainerStatus.Running,
        }:
            raise AuthorizationDeniedError("image archive download assignment is not active")
        if state.workspace_id != request.workspace_id:
            raise AuthorizationDeniedError(
                "image archive download workspace does not match container"
            )
        if state.worker_id != principal.worker_id:
            raise AuthorizationDeniedError("image archive download is bound to the assigned worker")
        if state.stub_id != request.stub_id or state.image_id != request.image_id:
            raise AuthorizationDeniedError(
                "image archive download does not match container assignment"
            )
        credentials = self.origin_credentials.vend(
            request,
            principal=principal.credential_principal(
                proven_workspace_id=request.workspace_id,
            ),
        )
        return GetCacheOriginCredentialsResponse(credentials=credentials)

    def get_image_archive_upload_credentials(
        self,
        request: ImageArchiveUploadCredentialRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> GetImageArchiveUploadCredentialsResponse:
        if not principal.worker_id:
            raise AuthorizationDeniedError("image archive upload requires a worker principal")
        self._authorize_worker_tenancy(
            principal,
            request.workspace_id,
            operation="image archive upload",
        )
        state = self.containers.get_container_state(request.container_id)
        if state is None:
            raise AuthorizationDeniedError("image archive upload container is not assigned")
        if state.workspace_id != request.workspace_id:
            raise AuthorizationDeniedError(
                "image archive upload workspace does not match container"
            )
        if state.worker_id != principal.worker_id:
            raise AuthorizationDeniedError("image archive upload is bound to the assigned worker")
        if state.image_build_id != request.build_id or state.image_id != request.image_id:
            raise AuthorizationDeniedError("image archive upload does not match the assigned build")
        if state.image_build_upload_capability != request.upload_capability:
            raise AuthorizationDeniedError(
                "image archive upload capability does not match assignment"
            )
        dependencies = self.dependencies
        if dependencies is None:
            raise UpstreamUnavailableError("image build service is required for archive upload")
        try:
            build = dependencies.images.get(
                request.build_id,
                workspace_id=request.workspace_id,
            )
        except NotFoundError as exc:
            raise AuthorizationDeniedError("image archive upload build is unavailable") from exc
        if build.image_id != request.image_id or build.status not in {
            BuildStatus.Pending,
            BuildStatus.Running,
        }:
            raise AuthorizationDeniedError(
                "image archive upload build ownership is no longer active"
            )
        if self.redis is None:
            raise UpstreamUnavailableError("Redis is required for image archive upload")
        if not RedisImageBuildUploadCapabilityGuard(self.redis).consume(request.upload_capability):
            raise AuthorizationDeniedError("image archive upload capability was already consumed")
        bucket, object_key = self.origin_credentials.upload_location(request)
        if not bucket or not object_key:
            raise UpstreamUnavailableError("image archive storage is not configured")
        reservation = dependencies.images.reserve_image_archive(
            request.image_id,
            object_key=object_key,
            size_bytes=request.archive_size_bytes,
            sha256=request.archive_sha256,
        )
        credentials = self.origin_credentials.vend_upload(
            request,
            principal=principal.credential_principal(
                proven_workspace_id=request.workspace_id,
            ),
            archive=reservation.archive,
            upload_required=reservation.upload_required,
        )
        if credentials.ok and (
            credentials.bucket != reservation.archive.bucket
            or credentials.object_key != reservation.archive.object_key
        ):
            raise RuntimeError("image archive upload descriptor does not match reservation")
        return GetImageArchiveUploadCredentialsResponse(credentials=credentials)

    def get_image_build_credentials(
        self,
        request: GetImageBuildCredentialsRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> GetImageBuildCredentialsResponse:
        if not principal.worker_id:
            raise AuthorizationDeniedError("image build credentials require a worker principal")
        self._authorize_worker_tenancy(
            principal,
            request.workspace_id,
            operation="image build credential",
        )
        state = self.containers.get_container_state(request.container_id)
        if state is None:
            return GetImageBuildCredentialsResponse()
        if state.workspace_id != request.workspace_id:
            raise AuthorizationDeniedError(
                "image build credential workspace does not match container"
            )
        if state.worker_id != principal.worker_id:
            raise AuthorizationDeniedError(
                "image build credentials are bound to the assigned worker"
            )
        if self.redis is None:
            raise UpstreamUnavailableError("Redis is required for image build credentials")
        private_inputs = RedisImageBuildCredentialCache(self.redis).consume(
            request.cache_key,
            workspace_id=request.workspace_id,
            build_id=request.build_id,
            container_id=request.container_id,
            registry=request.registry,
        )
        return GetImageBuildCredentialsResponse(private_inputs=private_inputs)

    def prepare_image_build_context_download(
        self,
        request: PrepareImageBuildContextDownloadRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> PrepareImageBuildContextDownloadResponse:
        if not principal.worker_id:
            raise AuthorizationDeniedError(
                "image build context download requires a worker principal"
            )
        self._authorize_worker_tenancy(
            principal,
            request.workspace_id,
            operation="image build context download",
        )
        state = self.containers.get_container_state(request.container_id)
        if state is None:
            raise AuthorizationDeniedError("image build context container is not assigned")
        if state.workspace_id != request.workspace_id:
            raise AuthorizationDeniedError("image build context workspace does not match container")
        if state.worker_id != principal.worker_id:
            raise AuthorizationDeniedError("image build context is bound to the assigned worker")
        if state.image_build_id != request.build_id:
            raise AuthorizationDeniedError("image build context does not match the assigned build")
        dependencies = self.dependencies
        if dependencies is None:
            raise UpstreamUnavailableError("image build service is required for context download")
        try:
            build = dependencies.images.get(
                request.build_id,
                workspace_id=request.workspace_id,
            )
        except NotFoundError as exc:
            raise AuthorizationDeniedError("image build context build is unavailable") from exc
        if (
            build.image.context_object_id != request.object_id
            or build.status not in {BuildStatus.Pending, BuildStatus.Running}
            or (state.image_id and state.image_id != build.image_id)
        ):
            raise AuthorizationDeniedError("image build context ownership is no longer active")
        object_storage = self.object_storage or dependencies.object_storage
        try:
            record = object_storage.get_by_id_for_workspace(
                request.object_id,
                workspace_id=request.workspace_id,
            )
        except NotFoundError as exc:
            raise AuthorizationDeniedError("image build context object is unavailable") from exc
        download_url = object_storage.generate_presigned_get_url_for_workspace(
            workspace_id=request.workspace_id,
            bucket=record.bucket,
            key=record.key,
            expires_seconds=IMAGE_BUILD_CONTEXT_DOWNLOAD_SECONDS,
        )
        return PrepareImageBuildContextDownloadResponse(
            object_id=record.id,
            download_url=download_url,
            content_length=record.size,
            sha256=record.sha256,
            expires_at=utc_now() + timedelta(seconds=IMAGE_BUILD_CONTEXT_DOWNLOAD_SECONDS),
        )

    def get_container_credentials(
        self,
        request: ContainerCredentialRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> GetContainerCredentialsResponse:
        if not principal.worker_id:
            raise AuthorizationDeniedError("container credentials require a worker principal")
        self._authorize_worker_tenancy(
            principal,
            request.workspace_id,
            operation="container credential",
        )
        state = self.containers.get_container_state(request.container_id)
        if state is None:
            raise AuthorizationDeniedError("container credentials require an assigned container")
        if state.workspace_id != request.workspace_id:
            raise AuthorizationDeniedError(
                "container credential workspace does not match container"
            )
        if state.worker_id != principal.worker_id:
            raise AuthorizationDeniedError("container credentials are bound to the assigned worker")
        return GetContainerCredentialsResponse(
            credentials=self.container_credentials.vend(
                request,
                principal=principal.credential_principal(
                    proven_workspace_id=request.workspace_id,
                ),
            )
        )

    def save_checkpoint_state(
        self,
        request: SaveCheckpointStateRequest,
    ) -> SaveCheckpointStateResponse:
        if self.services is None:
            raise UpstreamUnavailableError("service dependencies are required for checkpoint state")
        checkpoint = self.services.checkpoints.save_state(request.payload)
        return SaveCheckpointStateResponse(checkpoint=checkpoint)

    def acquire_automatic_checkpoint_lease(
        self,
        request: AcquireAutomaticCheckpointLeaseRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> AcquireAutomaticCheckpointLeaseResponse:
        self._authorize_automatic_checkpoint_lease(request, principal=principal)
        lease = self._automatic_checkpoint_lease_service().acquire(
            workspace_id=request.workspace_id,
            stub_id=request.stub_id,
            owner_token=request.container_id,
            ttl_seconds=request.ttl_seconds,
        )
        return AcquireAutomaticCheckpointLeaseResponse(
            acquired=lease.acquired,
            available_checkpoint_id=lease.available_checkpoint_id,
        )

    def release_automatic_checkpoint_lease(
        self,
        request: ReleaseAutomaticCheckpointLeaseRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> ReleaseAutomaticCheckpointLeaseResponse:
        self._authorize_automatic_checkpoint_lease(request, principal=principal)
        return ReleaseAutomaticCheckpointLeaseResponse(
            released=self._automatic_checkpoint_lease_service().release(
                workspace_id=request.workspace_id,
                stub_id=request.stub_id,
                owner_token=request.container_id,
            )
        )

    def _authorize_automatic_checkpoint_lease(
        self,
        request: AcquireAutomaticCheckpointLeaseRequest | ReleaseAutomaticCheckpointLeaseRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> None:
        if not principal.worker_id:
            raise AuthorizationDeniedError("automatic checkpoint lease requires a worker principal")
        self._authorize_worker_tenancy(
            principal,
            request.workspace_id,
            operation="automatic checkpoint lease",
        )
        state = self.containers.get_container_state(request.container_id)
        if state is None:
            raise AuthorizationDeniedError("automatic checkpoint lease container is not assigned")
        if state.workspace_id != request.workspace_id or state.stub_id != request.stub_id:
            raise AuthorizationDeniedError(
                "automatic checkpoint lease does not match the container"
            )
        if state.worker_id != principal.worker_id:
            raise AuthorizationDeniedError(
                "automatic checkpoint lease is bound to the assigned worker"
            )

    def _automatic_checkpoint_lease_service(
        self,
    ) -> AutomaticCheckpointCreationLeaseService:
        if self.services is None or self.redis is None:
            raise UpstreamUnavailableError(
                "service dependencies are required for automatic checkpoint leases"
            )
        return AutomaticCheckpointCreationLeaseService(self.services.context, self.redis)

    def get_checkpoint_restore(
        self,
        request: GetCheckpointRestoreRequest,
    ) -> GetCheckpointRestoreResponse:
        if self.services is None:
            raise UpstreamUnavailableError(
                "service dependencies are required for checkpoint restore"
            )
        if not request.checkpoint_bucket:
            raise InvalidInputError("checkpoint bucket is required")
        checkpoint = self.services.checkpoints.get_for_restore(
            request.checkpoint_id,
            workspace_id=request.workspace_id,
        )
        if not checkpoint.origin_key:
            raise ConflictError("checkpoint archive origin is unavailable")
        object_storage = self.object_storage or ObjectStorage(self.services.context)
        try:
            object_storage.get_for_workspace(
                workspace_id=request.workspace_id,
                bucket=request.checkpoint_bucket,
                key=checkpoint.origin_key,
            )
        except NotFoundError as exc:
            raise NotFoundError("checkpoint archive is unavailable") from exc
        download_url = object_storage.generate_presigned_get_url_for_workspace(
            workspace_id=request.workspace_id,
            bucket=request.checkpoint_bucket,
            key=checkpoint.origin_key,
            expires_seconds=900,
        )
        return GetCheckpointRestoreResponse(
            checkpoint=checkpoint,
            download_url=download_url,
        )

    def persist_checkpoint_archive(
        self,
        request: PersistCheckpointArchiveRequest,
    ) -> PersistCheckpointArchiveResponse:
        if self.services is None:
            raise UpstreamUnavailableError(
                "service dependencies are required for checkpoint archive persistence"
            )
        if not request.checkpoint_bucket:
            raise InvalidInputError("checkpoint bucket is required")
        object_storage = self.object_storage or ObjectStorage(self.services.context)
        checkpoint = self.services.checkpoints.get(request.checkpoint_id)
        if checkpoint is None or not checkpoint.workspace_id:
            raise NotFoundError(f"checkpoint not found: {request.checkpoint_id}")
        with tempfile.NamedTemporaryFile(prefix=f"{NAME}-checkpoint-", suffix=".tar") as temp:
            source = Path(temp.name)
            object_storage.download_file_for_workspace(
                workspace_id=checkpoint.workspace_id,
                bucket=request.checkpoint_bucket,
                key=request.origin_key,
                target=source,
            )
            actual_hash, actual_size = _sha256_file(source)
            if request.cache_hash and actual_hash != request.cache_hash:
                raise InvalidInputError("checkpoint archive hash does not match uploaded object")
            if request.cache_size_bytes and actual_size != request.cache_size_bytes:
                raise InvalidInputError("checkpoint archive size does not match uploaded object")
            (self.cache_storage or CacheStorage(self.services.context)).put(
                request.cache_namespace,
                actual_hash or request.origin_key,
                source,
            )
        return PersistCheckpointArchiveResponse(
            checkpoint_id=request.checkpoint_id,
            origin_key=request.origin_key,
            cache_hash=actual_hash,
            cache_size_bytes=actual_size,
            locality=request.locality,
            accelerator=request.accelerator,
        )

    def prepare_checkpoint_archive_upload(
        self,
        request: PrepareCheckpointArchiveUploadRequest,
    ) -> PrepareCheckpointArchiveUploadResponse:
        if self.services is None:
            raise UpstreamUnavailableError(
                "service dependencies are required for checkpoint archive upload"
            )
        if not request.checkpoint_bucket:
            raise InvalidInputError("checkpoint bucket is required")
        if not request.origin_key or not request.cache_hash or request.cache_size_bytes <= 0:
            raise InvalidInputError("checkpoint archive metadata is incomplete")
        object_storage = self.object_storage or ObjectStorage(self.services.context)
        checkpoint = self.services.checkpoints.get(request.checkpoint_id)
        if checkpoint is None or not checkpoint.workspace_id:
            raise NotFoundError(f"checkpoint not found: {request.checkpoint_id}")
        object_storage.reserve_for_workspace(
            workspace_id=checkpoint.workspace_id,
            bucket=request.checkpoint_bucket,
            key=request.origin_key,
            size=request.cache_size_bytes,
            sha256=request.cache_hash,
            content_type="application/x-tar",
            metadata={"checkpoint_id": request.checkpoint_id},
            overwrite=True,
        )
        return PrepareCheckpointArchiveUploadResponse(
            upload_url=object_storage.generate_presigned_put_url_for_workspace(
                workspace_id=checkpoint.workspace_id,
                bucket=request.checkpoint_bucket,
                key=request.origin_key,
                expires_seconds=900,
                content_length=request.cache_size_bytes,
                content_type="application/x-tar",
            ),
        )

    def publish_worker_event(
        self,
        request: PublishWorkerEventRequest,
    ) -> PublishWorkerEventResponse:
        if self.services is None:
            raise UpstreamUnavailableError("service dependencies are required for worker events")
        record = self.services.worker_events.append(request.record)
        return PublishWorkerEventResponse(record=record)

    def record_worker_usage(
        self,
        request: RecordWorkerUsageRequest,
        *,
        worker_id: str,
    ) -> RecordWorkerUsageResponse:
        services = self.services
        if services is None:
            raise UpstreamUnavailableError("service dependencies are required for worker usage")
        accepted = self._authorized_usage_record(request.record, worker_id=worker_id)
        record = services.usage.append(accepted)
        return RecordWorkerUsageResponse(record=record)

    def _authorized_usage_record(
        self,
        record: UsageRecord,
        *,
        worker_id: str,
    ) -> UsageRecord:
        """Refuse a usage record that is not this worker's to report.

        The record arrives entirely worker-authored — workspace, quantity, window,
        and the labels that decide who pays — and a worker token lives on a
        machine a customer joined and has root on. Taking the workspace on trust
        is what lets one account post GPU-seconds against another's, or relabel
        its own as self-hosted and have the rollup drop them.

        The window is worker-authored too, and the ledger prices from it, so the
        accepted record carries a window bounded by the lifetime the control
        plane recorded rather than the one the worker claimed.
        """

        if record.resource_type != _CONTAINER_RESOURCE:
            raise AuthorizationDeniedError(
                f"a worker may only record container usage, not {record.resource_type!r}"
            )
        self._authorize_worker_container(
            record.resource_id,
            worker_id=worker_id,
            operation="usage",
            workspace_id=record.workspace_id,
        )
        container = self._container_across_workspaces(record.resource_id, operation="usage")
        if container is None:
            # Still assigned in scheduler state but no durable row to bound it
            # against. Accepted rather than dropped: usage that was measured is
            # what this path exists to keep.
            return record
        return self._metered_within_lifetime(record, container, worker_id=worker_id)

    def _authorize_worker_container(
        self,
        container_id: str,
        *,
        worker_id: str,
        operation: str,
        workspace_id: str = "",
    ) -> None:
        """Refuse a container this worker was not given.

        A worker token authenticates a machine a customer joined and holds root
        on, so the container id in its request proves nothing by itself: without
        this, one worker can mark another tenant's container exited, delete the
        state that keeps it alive, or bill against it.

        The container is the fact that settles it, and the control plane already
        holds it: it placed that container on a worker, in a workspace. Resolved
        across workspaces on purpose, so the answer does not come from the same
        claim being checked.

        Placement is read the way `_authorize_network_container` reads it —
        scheduler state first, the durable row second — because those are the
        same question asked of the same dispatch. `runtime_worker_id` is the
        column that carries it: `worker_id` is set only for a private worker, so
        keying on it would authorize nothing for the platform fleet.
        """

        if not worker_id:
            raise AuthorizationDeniedError(f"{operation} requires an authenticated worker")
        state = self.containers.get_container_state(container_id)
        if state is not None:
            if state.worker_id != worker_id:
                raise AuthorizationDeniedError(
                    f"{operation} names a container assigned to another worker"
                )
            if workspace_id and state.workspace_id != workspace_id:
                raise AuthorizationDeniedError(
                    f"{operation} names a workspace the container does not belong to"
                )
            return
        container = self._container_across_workspaces(container_id, operation=operation)
        if container is None:
            raise AuthorizationDeniedError(
                f"{operation} names a container the platform did not reserve"
            )
        if container.runtime_worker_id != worker_id:
            raise AuthorizationDeniedError(
                f"{operation} names a container assigned to another worker"
            )
        if workspace_id and container.workspace_id != workspace_id:
            raise AuthorizationDeniedError(
                f"{operation} names a workspace the container does not belong to"
            )

    def _container_across_workspaces(
        self,
        container_id: str,
        *,
        operation: str,
    ) -> ContainerRecord | None:
        if self.services is None:
            raise UpstreamUnavailableError(
                f"{operation} cannot be resolved without the container record"
            )
        with self.services.context.database.session() as session:
            return ContainerRepository(session).get_across_workspaces(container_id)

    def _metered_within_lifetime(
        self,
        record: UsageRecord,
        container: ContainerRecord,
        *,
        worker_id: str,
    ) -> UsageRecord:
        """Bound the billed window by the lifetime the control plane holds.

        A worker states when its window opened and closed, and every reader of
        the record downstream — the ledger's split across rate changes, the
        period a charge lands in — takes those instants at their word. The
        container row is the platform's own account of when the work could have
        been running, so a window reaching outside it is cut back to it and one
        lying entirely outside is refused.
        """

        window = _metering_window(record)
        if window is None:
            return record
        started_at, ended_at = window
        earliest = to_utc(container.started_at or container.created_at) - _METERING_WINDOW_TOLERANCE
        latest = to_utc(container.finished_at or utc_now()) + _METERING_WINDOW_TOLERANCE
        if ended_at <= earliest or started_at >= latest:
            raise AuthorizationDeniedError(
                "usage names a window outside the container's recorded lifetime"
            )
        bounded_start = max(started_at, earliest)
        bounded_end = min(ended_at, latest)
        if bounded_end <= bounded_start:
            raise AuthorizationDeniedError(
                "usage names a window outside the container's recorded lifetime"
            )
        if (bounded_start, bounded_end) == (started_at, ended_at):
            return record
        LOGGER.warning(
            "clamped worker usage window to the container lifetime",
            extra={
                "container_id": container.id,
                "worker_id": worker_id,
                "reported_window": f"{started_at.isoformat()}/{ended_at.isoformat()}",
                "bounded_window": f"{bounded_start.isoformat()}/{bounded_end.isoformat()}",
            },
        )
        return record.model_copy(
            update={
                "metadata": {
                    **record.metadata,
                    METERING_WINDOW_STARTED_AT_METADATA_KEY: bounded_start.isoformat(),
                    METERING_WINDOW_ENDED_AT_METADATA_KEY: bounded_end.isoformat(),
                }
            }
        )

    def publish_container_lifecycle(
        self,
        request: PublishContainerLifecycleRequest,
    ) -> PublishContainerLifecycleResponse:
        event_streams = self._event_streams()
        if event_streams is None:
            raise UpstreamUnavailableError("redis is required for container lifecycle events")
        event = event_streams.append_event(
            EventRecordType.ContainerLifecycle,
            request.payload.model_dump(mode="python"),
        )
        self._record_container_startup_failure(request.payload)
        return PublishContainerLifecycleResponse(
            event=event,
        )

    def publish_container_metrics(
        self,
        request: PublishContainerMetricsRequest,
    ) -> PublishContainerMetricsResponse:
        event_streams = self._event_streams()
        if event_streams is None:
            raise UpstreamUnavailableError("redis is required for container metrics events")
        return PublishContainerMetricsResponse(
            event=event_streams.append_event(
                EventRecordType.ContainerMetrics,
                request.payload.model_dump(mode="python"),
            )
        )

    def publish_container_event(
        self,
        request: PublishContainerEventRequest,
    ) -> PublishContainerEventResponse:
        event_streams = self._event_streams()
        if event_streams is None:
            raise UpstreamUnavailableError("redis is required for container events")
        return PublishContainerEventResponse(
            event=event_streams.append_event(
                EventRecordType.ContainerEvent,
                request.payload.model_dump(mode="python"),
            )
        )

    def append_sandbox_process_log(
        self,
        request: AppendSandboxProcessLogRequest,
    ) -> AppendSandboxProcessLogResponse:
        event_streams = self._event_streams()
        if event_streams is None:
            raise UpstreamUnavailableError("redis is required for sandbox process logs")
        return AppendSandboxProcessLogResponse(
            event=event_streams.append_event(
                EventRecordType.ContainerLog,
                request.entry.model_dump(mode="python"),
            )
        )

    def append_container_logs(
        self,
        request: AppendContainerLogsRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> AppendContainerLogsResponse:
        if self.services is None:
            raise UpstreamUnavailableError(
                "service dependencies are required for container log ingestion"
            )
        event_streams = self._event_streams()
        if event_streams is None:
            raise UpstreamUnavailableError("redis is required for container log ingestion")
        if not principal.worker_id:
            raise ConflictError("authenticated worker identity is not bound to a worker")
        ingestion = ContainerLogIngestionService(
            self.services.context,
            event_streams,
        )
        if principal.is_managed_worker:
            worker = self.workers.get_worker(principal.worker_id)
            state = self.containers.get_container_state(request.container_id)
            if worker is None:
                raise ConflictError("container log runtime assignment is unavailable")
            if state is None:
                with self.services.context.database.session() as session:
                    container = ContainerRepository(session).get_across_workspaces(
                        request.container_id
                    )
                if (
                    container is None
                    or container.runtime_worker_id != principal.worker_id
                    or container.runtime_machine_id != worker.machine_id
                ):
                    raise ConflictError("container log runtime assignment is unavailable")
            elif state.worker_id != principal.worker_id:
                raise ConflictError(
                    "container log runtime assignment does not match the authenticated worker"
                )
            result = ingestion.append_runtime_batch(
                container_id=request.container_id,
                capture_id=request.capture_id,
                entries=request.entries,
                attribution=ContainerLogRuntimeAttribution(
                    worker_id=principal.worker_id,
                    machine_id=worker.machine_id,
                ),
            )
        else:
            result = ingestion.append_batch(
                container_id=request.container_id,
                capture_id=request.capture_id,
                entries=request.entries,
                expected_worker_id=principal.worker_id,
            )
        return AppendContainerLogsResponse(
            accepted_through=result.accepted_through,
            appended_count=result.appended_count,
        )

    def set_network_lock(
        self,
        request: NetworkLockRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> NetworkLockResponse:
        network_prefix, _ = self._authorized_network_scope(principal)
        return NetworkLockResponse(
            lock=self.network.set_network_lock(
                network_prefix,
                ttl_seconds=request.ttl_seconds,
                retries=request.retries,
            )
        )

    def remove_network_lock(
        self,
        request: RemoveNetworkLockRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> RemoveNetworkLockResponse:
        network_prefix, _ = self._authorized_network_scope(principal)
        return RemoveNetworkLockResponse(
            release=self.network.remove_network_lock(network_prefix, request.token)
        )

    def set_container_ip(
        self,
        request: SetContainerIpRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> SetContainerIpResponse:
        network_prefix, worker = self._authorized_network_scope(principal)
        self._authorize_network_container(principal, worker, request.container_id)
        return SetContainerIpResponse(
            plan=self.network.set_container_ip(
                network_prefix,
                request.container_id,
                request.ip_address,
            )
        )

    def move_container_ip(
        self,
        request: MoveContainerIpRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> MoveContainerIpResponse:
        network_prefix, worker = self._authorized_network_scope(principal)
        self._authorize_network_container(principal, worker, request.from_container_id)
        self._authorize_network_container(principal, worker, request.to_container_id)
        return MoveContainerIpResponse(
            plan=self.network.move_container_ip(
                network_prefix,
                request.from_container_id,
                request.to_container_id,
                request.ip_address,
            )
        )

    def get_container_ip(
        self,
        request: GetContainerIpRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> GetContainerIpResponse:
        network_prefix, worker = self._authorized_network_scope(principal)
        self._authorize_network_container(principal, worker, request.container_id)
        return GetContainerIpResponse(
            ip_address=self.network.get_container_ip(
                network_prefix,
                request.container_id,
            )
            or ""
        )

    def get_container_ips(
        self,
        request: GetContainerIpsRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> GetContainerIpsResponse:
        network_prefix, _ = self._authorized_network_scope(principal)
        return GetContainerIpsResponse(ip_addresses=tuple(self.network.list_ips(network_prefix)))

    def get_container_ip_assignments(
        self,
        request: GetContainerIpAssignmentsRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> GetContainerIpAssignmentsResponse:
        network_prefix, _ = self._authorized_network_scope(principal)
        return GetContainerIpAssignmentsResponse(
            assignments=tuple(self.network.list_assignments(network_prefix))
        )

    def remove_container_ip(
        self,
        request: RemoveContainerIpRequest,
        *,
        principal: WorkerRepositoryPrincipal,
    ) -> RemoveContainerIpResponse:
        network_prefix, worker = self._authorized_network_scope(principal)
        self._authorize_network_container(principal, worker, request.container_id)
        return RemoveContainerIpResponse(
            plan=self.network.remove_container_ip(
                network_prefix,
                request.container_id,
            )
        )

    def _authorized_network_scope(
        self,
        principal: WorkerRepositoryPrincipal,
    ) -> tuple[str, SchedulerWorkerRecord]:
        if not principal.worker_id:
            raise AuthorizationDeniedError(
                "network mutation requires an authenticated worker identity"
            )
        worker = self._validate_worker_stream(principal.worker_id, principal=principal)
        if not worker.machine_id:
            raise AuthorizationDeniedError("network mutation requires a registered worker machine")
        return (
            worker_network_prefix(worker.capacity_owner_id, worker.machine_id),
            worker,
        )

    def _authorize_network_container(
        self,
        principal: WorkerRepositoryPrincipal,
        worker: SchedulerWorkerRecord,
        container_id: str,
    ) -> None:
        if container_id == worker.worker_id:
            # A worker reserving an address for itself, which readiness does
            # before any container exists. The principal already proves this
            # worker, and the prefix is already its own, so there is nothing
            # further to authorize against.
            return
        state = self.containers.get_container_state(container_id)
        if state is not None:
            if state.worker_id != worker.worker_id:
                raise AuthorizationDeniedError("network mutation is bound to the assigned worker")
            self._authorize_worker_tenancy(
                principal,
                state.workspace_id,
                operation="network mutation",
            )
            return
        if self.services is not None:
            with self.services.context.database.session() as session:
                container = ContainerRepository(session).get_across_workspaces(container_id)
            if (
                container is not None
                and container.runtime_worker_id == worker.worker_id
                and container.runtime_machine_id == worker.machine_id
            ):
                self._authorize_worker_tenancy(
                    principal,
                    container.workspace_id,
                    operation="network mutation",
                )
                return
        raise AuthorizationDeniedError("network mutation is bound to the assigned worker container")

    def _worker_event_from_id(self, event_id: str) -> WorkerStreamEvent | None:
        if event_id == WORKER_EVENT_HEARTBEAT_ID:
            return _heartbeat_event()
        claim = self.events.claim(event_id)
        plan = worker_stream_event_from_bus_event(event_id=event_id, event=claim.event)
        if not plan.converted or plan.event is None:
            return None
        return plan.event

    def _pending_worker_event_ids(self, worker_id: str) -> list[str]:
        if self.redis is None or not worker_id:
            return []
        return sorted(
            redis_text(event_id)
            for event_id in self.redis.set_members(self._pending_event_key(worker_id))
        )

    def _pending_event_key(self, worker_id: str) -> str:
        if self.redis is None:
            return ""
        return self.redis.key("worker-events", "pending", worker_id)

    def _event_ack_key(self, event_id: str) -> str:
        if self.redis is None:
            return ""
        return self.redis.key("worker-events", "ack", event_id)

    def _pubsub_worker_event_ids(
        self,
        request: StreamWorkerEventsRequest,
    ) -> Iterator[str]:
        if self.redis is None:
            return
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        channels = [
            self.redis.key(event_channel_key(EventBusEventType.StopContainer)),
            self.redis.key(event_channel_key(EventBusEventType.StopBuild)),
            self.redis.key(event_channel_key(EventBusEventType.PurgeSourceCache)),
        ]
        pubsub.subscribe(*channels)
        emitted = 0
        deadline = time.monotonic() + max(request.heartbeat_interval_seconds, 0.1)
        try:
            while request.max_events <= 0 or emitted < request.max_events:
                message = pubsub.get_message(timeout=max(request.pubsub_timeout_seconds, 0.01))
                if not message:
                    if time.monotonic() >= deadline:
                        yield WORKER_EVENT_HEARTBEAT_ID
                        emitted += 1
                        deadline = time.monotonic() + max(
                            request.heartbeat_interval_seconds,
                            0.1,
                        )
                    continue
                if redis_text(message.type) != "message":
                    continue
                event_id = redis_text(message.data)
                if not self._event_targets_worker(event_id, request.worker_id):
                    continue
                yield event_id
                emitted += 1
        finally:
            pubsub.close()

    def _event_targets_worker(self, event_id: str, worker_id: str) -> bool:
        claim = self.events.claim(event_id)
        event = claim.event
        if event is None:
            return False
        target = event.args.get("worker_id")
        if event.type == EventBusEventType.StopContainer:
            return isinstance(target, str) and bool(target.strip()) and target == worker_id
        return not isinstance(target, str) or not target.strip() or target == worker_id

    def _event_streams(self) -> RedisEventStreamRepository | None:
        if self.redis is None:
            return None
        return RedisEventStreamRepository(self.redis)

    def _sync_runtime_container_status(
        self,
        container_id: str,
        status: SchedulerContainerStatus,
    ) -> None:
        if self.services is None:
            return
        container_status = _runtime_container_status_from_scheduler(status)
        if container_status is None:
            return
        now = utc_now()
        updated_task: Task | None = None
        with self.services.context.database.session() as session:
            container = ContainerRepository(session).get_across_workspaces(container_id)
            if container is None:
                return
            previous_state = (
                container.status,
                container.started_at,
                container.finished_at,
            )
            if container.status is ContainerStatus.Stopped:
                container.finished_at = container.finished_at or now
            else:
                container.status = container_status
                if container_status is ContainerStatus.Running and container.started_at is None:
                    container.started_at = now
                if container_status in TERMINAL_CONTAINER_STATUSES:
                    container.finished_at = container.finished_at or now
                    self._release_container_runtime_state(container)
                    updated_task = self._sync_runtime_task_for_container_terminal_state(
                        session,
                        container,
                        status=TaskStatus.Complete
                        if container_status is ContainerStatus.Exited
                        else TaskStatus.Failed,
                        exit_code=container.exit_code,
                        error=None
                        if container_status is ContainerStatus.Exited
                        else f"container {container.id} exited before task completion",
                        finished_at=container.finished_at,
                    )
            changed = previous_state != (
                container.status,
                container.started_at,
                container.finished_at,
            )
            if changed:
                container = ContainerRepository(session).upsert(container)
        if changed:
            self._publish_runtime_container_change(container)
        if updated_task is not None:
            self._publish_runtime_task_change(container, updated_task)

    def _sync_runtime_container_exit(
        self,
        container_id: str,
        exit_code: int,
        *,
        termination_reason: StopContainerReason,
        failed_phase: ContainerExecutionPhase | None = None,
        failure_detail: str = "",
    ) -> None:
        if self.services is None:
            return
        now = utc_now()
        updated_task: Task | None = None
        preempted = termination_reason is StopContainerReason.Preempted
        reconcile_preemption = False
        settle_required = False
        crashed_task_ids: list[str] = []
        with self.services.context.database.session() as session:
            container = ContainerRepository(session).get_across_workspaces(container_id)
            if container is None:
                return
            reconcile_preemption = preempted and container.status is not ContainerStatus.Stopped
            previous_state = (
                container.status,
                container.exit_code,
                container.termination_reason,
                container.started_at,
                container.finished_at,
                container.preemption_settled_at,
            )
            # Never back to Unknown. Several worker paths report it as a
            # placeholder, and the row may already hold the reason the stop was
            # asked for — which is the one the customer is owed.
            if (
                termination_reason is not StopContainerReason.Unknown
                or container.termination_reason is StopContainerReason.Unknown
            ):
                container.termination_reason = termination_reason
            if container.status is ContainerStatus.Stopped:
                container.exit_code = exit_code
                container.started_at = container.started_at or now
                container.finished_at = container.finished_at or now
            else:
                container.exit_code = exit_code
                container.status = (
                    ContainerStatus.Exited if exit_code == 0 else ContainerStatus.Failed
                )
                container.started_at = container.started_at or now
                container.finished_at = container.finished_at or now
                self._release_container_runtime_state(container)
                if not preempted:
                    updated_task = self._sync_runtime_task_for_container_terminal_state(
                        session,
                        container,
                        status=TaskStatus.Complete if exit_code == 0 else TaskStatus.Failed,
                        exit_code=exit_code,
                        error=None
                        if exit_code == 0
                        else _container_exit_error(
                            container.id,
                            exit_code,
                            failed_phase=failed_phase,
                            failure_detail=failure_detail,
                        ),
                        finished_at=container.finished_at,
                    )
            # Whatever the container had claimed goes back to the pool. A pooled
            # container carries no task id of its own, so the sync above — which
            # settles the task the container was created for — cannot see its
            # work, and an exit nobody asked for would otherwise leave the task
            # naming a container that is gone: invisible to a claim, unreachable
            # by a retry, and waited on forever by its caller.
            #
            # Except where the container died on its own having failed, which is
            # the one exit the work itself may have caused. Handed straight back,
            # an invocation that kills its interpreter is claimed again, kills
            # the next container the same way, and is handed back again — a loop
            # nothing bounds, because a claim returning to an attempt still
            # marked running never advances `attempt_number` and so never
            # reaches `max_attempts`. Charged an attempt it retries on the usual
            # terms and fails for good when they run out. Anything the platform
            # stopped keeps its budget: that was not the caller's doing.
            if termination_reason is StopContainerReason.Unknown and exit_code != 0:
                crashed_task_ids = [
                    task.id
                    for task in TaskRepository(session).list_inflight_for_container(container.id)
                    if task.id != container.task_id
                ]
            else:
                self._release_pooled_claims(session, container)
            # The preemption retry intent must commit with the terminal state it belongs to.
            # A container that needs no settling is marked settled here so the recovery
            # sweep only ever sees work that is genuinely outstanding. A pooled
            # container's claims were just released, so it needs no second pass.
            settle_required = bool(reconcile_preemption and container.task_id)
            if preempted and not settle_required:
                container.preemption_settled_at = container.preemption_settled_at or now
            changed = previous_state != (
                container.status,
                container.exit_code,
                container.termination_reason,
                container.started_at,
                container.finished_at,
                container.preemption_settled_at,
            )
            if changed:
                container = ContainerRepository(session).upsert(container)
        if changed:
            self._publish_runtime_container_change(container)
        for task_id in crashed_task_ids:
            self.services.tasks.finish_with_retry(
                task_id,
                TaskStatus.Failed,
                container_id=container.id,
                error=_container_exit_error(
                    container.id,
                    exit_code,
                    failed_phase=failed_phase,
                    failure_detail=failure_detail,
                ),
                exit_code=exit_code,
            )
        if settle_required:
            self.services.preempted_containers.preempted(container, exit_code=exit_code)
            self._mark_container_preemption_settled(container.id)
        if updated_task is not None:
            self._publish_runtime_task_change(container, updated_task)

    def _release_pooled_claims(
        self,
        session: DatabaseSession,
        container: ContainerRecord,
    ) -> None:
        """Give back the invocations a pooled container was holding when it died.

        Read from the task side because the claim is the only record: a function
        container is started for its stub and its `task_id` stays empty for its
        whole life. Released rather than failed, and released with its retry
        budget untouched — the platform took this container away, so the caller's
        invocation is still wanted and has spent nothing. The caller sees only
        that it ran somewhere else.
        """

        TaskRepository(session).release_claims_for_container(
            container.id,
            except_task_id=container.task_id,
        )

    def _mark_container_preemption_settled(self, container_id: str) -> None:
        if self.services is None:
            return
        with self.services.context.database.session() as session:
            ContainerRepository(session).mark_preemption_settled(container_id, now=utc_now())

    def _release_container_runtime_state(self, container: ContainerRecord) -> None:
        """A worker reporting an exit is a terminal transition like any other.

        These are the only paths that ever write Exited, and they run from worker
        callbacks rather than through the container service, so the release has
        to be made here too or a container that simply finished keeps its
        keep-warm marker and its share of the deployment's connection count.
        """
        if self.runtime_state is None or not container.stub_id:
            return
        try:
            self.runtime_state.release(
                workspace_id=container.workspace_id,
                stub_id=container.stub_id,
                container_id=container.id,
            )
        except Exception:
            LOGGER.warning(
                "releasing container runtime state failed",
                exc_info=True,
                extra={"container_id": container.id},
            )

    def _sync_runtime_task_for_container_terminal_state(
        self,
        session: DatabaseSession,
        container: ContainerRecord,
        *,
        status: TaskStatus,
        exit_code: int | None,
        error: str | None,
        finished_at: datetime | None,
    ) -> Task | None:
        task_id = getattr(container, "task_id", "") or ""
        if not task_id:
            return None
        task = TaskRepository(session).get_across_workspaces(task_id)
        if (
            task is None
            or is_terminal_task_status(task.status)
            or task.status is TaskStatus.Retry
            or (task.container_id is not None and str(task.container_id) != str(container.id))
        ):
            return None
        task.status = status
        task.exit_code = exit_code
        task.finished_at = task.finished_at or finished_at
        if getattr(container, "id", ""):
            task.kwargs.setdefault("container_id", str(container.id))
        if error:
            task.error = error
        return TaskRepository(session).upsert(
            task,
            workspace_id=getattr(container, "workspace_id", None),
        )

    def _record_container_startup_failure(self, payload: ContainerLifecyclePayload) -> None:
        if payload.success is not False or payload.id not in FATAL_CONTAINER_STARTUP_PHASES:
            return
        if not payload.container_id:
            return
        try:
            self.containers.set_exit_code(payload.container_id, 1)
            plan = self.containers.update_container_status(
                payload.container_id,
                SchedulerContainerStatus.Failed,
            )
            state = self.containers.get_container_state(payload.container_id)
            if plan.release_concurrency and state is not None and state.worker_id:
                with suppress(SchedulerRepositoryError):
                    self.workers.reconcile_worker_capacity(state.worker_id)
        except SchedulerRepositoryError:
            pass
        if self.services is None:
            return

        finished_at = payload.end_time or utc_now()
        error = _container_startup_failure_error(payload)
        changed_container: ContainerRecord | None = None
        changed_task: Task | None = None
        with self.services.context.database.session() as session:
            container = ContainerRepository(session).get_across_workspaces(payload.container_id)
            task_id = payload.task_id or (container.task_id if container is not None else "")

            if container is not None:
                previous_state = (
                    container.task_id,
                    container.status,
                    container.exit_code,
                    container.finished_at,
                )
                if task_id and not container.task_id:
                    container.task_id = task_id
                container.status = ContainerStatus.Failed
                container.exit_code = 1
                container.finished_at = container.finished_at or finished_at
                self._release_container_runtime_state(container)
                if previous_state != (
                    container.task_id,
                    container.status,
                    container.exit_code,
                    container.finished_at,
                ):
                    changed_container = ContainerRepository(session).upsert(container)

            if task_id:
                task = TaskRepository(session).get_across_workspaces(task_id)
                if task is not None and not is_terminal_task_status(task.status):
                    if payload.container_id:
                        task.kwargs.setdefault("container_id", payload.container_id)
                    task.status = TaskStatus.Failed
                    task.error = error
                    task.exit_code = 1
                    task.finished_at = task.finished_at or finished_at
                    changed_task = TaskRepository(session).upsert(
                        task,
                        workspace_id=payload.workspace_id
                        or (container.workspace_id if container else None),
                    )
        publish_container = changed_container or container
        if changed_container is not None:
            self._publish_runtime_container_change(changed_container)
        if changed_task is not None and publish_container is not None:
            self._publish_runtime_task_change(publish_container, changed_task)

    def _publish_runtime_container_change(self, container: ContainerRecord) -> None:
        if self.services is None:
            return
        self.services.workspace_changes.emit_change(
            workspace_id=container.workspace_id,
            topic=WorkspaceChangeTopic.Containers,
            change=WorkspaceChangeType.Updated,
            resource_id=container.id,
            app_id=container.app_id,
            stub_id=container.stub_id,
            task_id=container.task_id or None,
            container_id=container.id,
        )

    def _publish_runtime_task_change(
        self,
        container: ContainerRecord,
        task: Task,
    ) -> None:
        if self.services is None:
            return
        self.services.workspace_changes.emit_change(
            workspace_id=container.workspace_id,
            topic=WorkspaceChangeTopic.Tasks,
            change=WorkspaceChangeType.Updated,
            resource_id=task.id,
            app_id=task.app_id or container.app_id,
            deployment_id=task.deployment_id,
            stub_id=task.stub_id or container.stub_id,
            task_id=task.id,
            root_task_id=task.root_task_id,
            container_id=container.id,
        )


def _container_exit_error(
    container_id: str,
    exit_code: int,
    *,
    failed_phase: ContainerExecutionPhase | None,
    failure_detail: str,
) -> str:
    """Describe why a container reached a terminal state.

    The asynchronous startup-failure lifecycle event reports the same thing, but
    arrives after this call has already made the task terminal and is dropped, so
    the reason has to travel with the exit code to reach the task owner.
    """
    if failed_phase is None:
        return f"container {container_id} exited with code {exit_code}"
    if failure_detail:
        return f"container startup failed during {failed_phase.value}: {failure_detail}"
    return f"container startup failed during {failed_phase.value}"


def _container_startup_failure_error(payload: ContainerLifecyclePayload) -> str:
    detail = payload.attrs.get("error") or payload.attrs.get("reason") or ""
    if detail:
        return f"container startup failed during {payload.id}: {detail}"
    return f"container startup failed during {payload.id}"


def _runtime_container_status_from_scheduler(
    status: SchedulerContainerStatus,
) -> ContainerStatus | None:
    match status:
        case SchedulerContainerStatus.Pending:
            return ContainerStatus.Pending
        case SchedulerContainerStatus.Running:
            return ContainerStatus.Running
        case SchedulerContainerStatus.Complete:
            return ContainerStatus.Exited
        case SchedulerContainerStatus.Failed:
            return ContainerStatus.Failed
        case SchedulerContainerStatus.Stopping:
            return None


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _heartbeat_event() -> WorkerStreamEvent:
    return WorkerStreamEvent(
        event_id=WORKER_EVENT_HEARTBEAT_ID,
        kind=WorkerStreamEventKind.Heartbeat,
    )


def _metering_window(record: UsageRecord) -> tuple[datetime, datetime] | None:
    """The interval the producer says its quantity covers, or nothing.

    Read exactly as the ledger reads it, down to refusing a naive timestamp: a
    bound this cannot resolve to an instant is one the ledger will not price
    either, so there is nothing here to hold to the container's lifetime.
    """

    started_at = _metering_instant(record.metadata.get(METERING_WINDOW_STARTED_AT_METADATA_KEY))
    ended_at = _metering_instant(record.metadata.get(METERING_WINDOW_ENDED_AT_METADATA_KEY))
    if started_at is None or ended_at is None or ended_at <= started_at:
        return None
    return (started_at, ended_at)


def _metering_instant(value: JsonValue) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return to_utc(moment)
