from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from identity.auth import AuthError, AuthorizationDeniedError
from identity.authz import worker_requirement
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.identity import AuthScope
from worker.events import WorkerStreamEvent
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
    ListImagePrewarmTargetsRequest,
    ListImagePrewarmTargetsResponse,
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
    WorkerCacheSessionRequest,
    WorkerContainerIndexRequest,
    WorkerContainerIndexResponse,
    WorkerIdRequest,
    WorkerKeepAliveResponse,
    WorkerRecordResponse,
    WorkerRepositoryPrincipal,
)
from worker.tools import ContainerCredentialRequest

from api.server.dependencies import (
    AuthorizationCredentials,
    authorization_header,
    current_services,
)
from api.server.service_dependencies import worker_repository_service
from api.server.services import ApiServices
from api.server.sse import sse_response
from api.server.worker_repository_service import WorkerRepositoryService

router = APIRouter(tags=["worker-repository"])


async def worker_repository_principal(
    services: Annotated[ApiServices, Depends(current_services)],
    credentials: AuthorizationCredentials = None,
) -> WorkerRepositoryPrincipal:
    try:
        io = services.require_async_io()
        principal = await services.auth.authorize_principal_async(
            io.database,
            io.auth_invalidation,
            authorization_header(credentials),
            worker_requirement(action=AuthScope.Worker),
            allow_if_no_tokens=False,
        )
    except AuthorizationDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "worker token is required")
    return WorkerRepositoryPrincipal.from_token(principal.token)


WorkerPrincipal = Annotated[WorkerRepositoryPrincipal, Depends(worker_repository_principal)]
WorkerRepo = Annotated[WorkerRepositoryService, Depends(worker_repository_service)]


def _require_worker_subject(
    principal: WorkerRepositoryPrincipal,
    worker_id: str,
    *,
    action: str,
) -> None:
    if not principal.worker_id or principal.worker_id != worker_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{action} does not match the authenticated worker",
        )


@router.post(
    "/worker-repository/get-next-container-request",
    response_class=StreamingResponse,
)
def get_next_container_request(
    request: GetNextContainerRequestRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
    services: Annotated[ApiServices, Depends(current_services)],
) -> StreamingResponse:
    _require_worker_subject(principal, request.worker_id, action="worker request stream")
    return sse_response(
        _container_request_items(
            service.stream_next_container_requests(
                services.require_async_io(),
                request,
                principal=principal,
            )
        )
    )


async def _container_request_items(
    requests: AsyncIterator[GetNextContainerRequestResponse],
) -> AsyncIterator[tuple[str, str, object]]:
    # The response has already started, so these cannot become a 409 or 503;
    # the worker reconnects and the next stream revalidates it.
    try:
        async for item in requests:
            yield ("container-request", "", item)
    except (ConflictError, UpstreamUnavailableError):
        return


@router.post(
    "/worker-repository/acknowledge-container-request",
    response_model=AcknowledgeContainerRequestResponse,
)
async def acknowledge_container_request(
    request: AcknowledgeContainerRequestRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
    services: Annotated[ApiServices, Depends(current_services)],
) -> AcknowledgeContainerRequestResponse:
    _require_worker_subject(
        principal, request.worker_id, action="container request acknowledgement"
    )
    return await service.acknowledge_container_request(services.require_async_io(), request)


@router.post("/worker-repository/stream-worker-events", response_class=StreamingResponse)
def stream_worker_events(
    request: StreamWorkerEventsRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
    services: Annotated[ApiServices, Depends(current_services)],
) -> StreamingResponse:
    _require_worker_subject(principal, request.worker_id, action="worker event stream")
    return sse_response(
        _worker_event_items(service.stream_worker_events(services.require_async_io(), request))
    )


async def _worker_event_items(
    events: AsyncIterator[WorkerStreamEvent],
) -> AsyncIterator[tuple[str, str, object]]:
    async for item in events:
        yield ("worker-event", "", item)


@router.post(
    "/worker-repository/acknowledge-worker-event",
    response_model=AcknowledgeWorkerEventResponse,
)
async def acknowledge_worker_event(
    request: AcknowledgeWorkerEventRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
    services: Annotated[ApiServices, Depends(current_services)],
) -> AcknowledgeWorkerEventResponse:
    _require_worker_subject(principal, request.worker_id, action="worker event acknowledgement")
    return await service.acknowledge_worker_event(services.require_async_io(), request)


@router.post(
    "/worker-repository/set-image-pull-lock",
    response_model=SetImagePullLockResponse,
)
def set_image_pull_lock(
    request: SetImagePullLockRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> SetImagePullLockResponse:
    _ = principal
    return service.set_image_pull_lock(request)


@router.post(
    "/worker-repository/remove-image-pull-lock",
    response_model=RemoveImagePullLockResponse,
)
def remove_image_pull_lock(
    request: RemoveImagePullLockRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> RemoveImagePullLockResponse:
    _ = principal
    return service.remove_image_pull_lock(request)


@router.post(
    "/worker-repository/add-container-to-worker",
    response_model=WorkerContainerIndexResponse,
)
def add_container_to_worker(
    request: WorkerContainerIndexRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> WorkerContainerIndexResponse:
    _require_worker_subject(principal, request.worker_id, action="worker container index update")
    return service.add_container_to_worker(request)


@router.post(
    "/worker-repository/remove-container-from-worker",
    response_model=WorkerContainerIndexResponse,
)
def remove_container_from_worker(
    request: WorkerContainerIndexRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> WorkerContainerIndexResponse:
    _require_worker_subject(principal, request.worker_id, action="worker container index update")
    return service.remove_container_from_worker(request)


@router.post("/worker-repository/get-worker-by-id", response_model=GetWorkerByIdResponse)
def get_worker_by_id(
    request: WorkerIdRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetWorkerByIdResponse:
    _require_worker_subject(principal, request.worker_id, action="worker lookup")
    return service.get_worker_by_id(request)


@router.post(
    "/worker-repository/list-image-prewarm-targets",
    response_model=ListImagePrewarmTargetsResponse,
)
def list_image_prewarm_targets(
    request: ListImagePrewarmTargetsRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> ListImagePrewarmTargetsResponse:
    _require_worker_subject(principal, request.worker_id, action="image prewarm target lookup")
    return service.list_image_prewarm_targets(request, principal=principal)


@router.post("/worker-repository/add-worker", response_model=WorkerRecordResponse)
def add_worker(
    request: AddWorkerRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> WorkerRecordResponse:
    if principal.worker_id and principal.worker_id != request.worker.worker_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "worker registration does not match the authenticated worker",
        )
    return service.add_worker(request, principal=principal)


@router.post(
    "/worker-repository/toggle-worker-available",
    response_model=WorkerRecordResponse,
)
def toggle_worker_available(
    request: WorkerCacheSessionRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> WorkerRecordResponse:
    _require_worker_subject(principal, request.worker_id, action="worker availability update")
    return service.toggle_worker_available(request, principal=principal)


@router.post("/worker-repository/disable-worker", response_model=WorkerRecordResponse)
def disable_worker(
    request: DisableWorkerRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> WorkerRecordResponse:
    _require_worker_subject(principal, request.worker_id, action="worker disable")
    return service.disable_worker(request)


@router.post("/worker-repository/remove-worker", response_model=RemoveWorkerResponse)
def remove_worker(
    request: WorkerIdRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> RemoveWorkerResponse:
    _require_worker_subject(principal, request.worker_id, action="worker removal")
    response = service.remove_worker(request)
    service.revoke_worker_session(principal)
    return response


@router.post(
    "/worker-repository/update-worker-capacity",
    response_model=UpdateWorkerCapacityResponse,
)
def update_worker_capacity(
    request: UpdateWorkerCapacityRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> UpdateWorkerCapacityResponse:
    _require_worker_subject(principal, request.worker_id, action="worker capacity update")
    return service.update_worker_capacity(request)


@router.post("/worker-repository/set-worker-keep-alive", response_model=WorkerKeepAliveResponse)
def set_worker_keep_alive(
    request: WorkerCacheSessionRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> WorkerKeepAliveResponse:
    _require_worker_subject(principal, request.worker_id, action="worker keepalive")
    return service.set_worker_keep_alive(request, principal=principal)


@router.post(
    "/worker-repository/claim-source-cache-cleanup",
    response_model=ClaimSourceCacheCleanupResponse,
)
def claim_source_cache_cleanup(
    request: ClaimSourceCacheCleanupRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> ClaimSourceCacheCleanupResponse:
    _require_worker_subject(principal, request.worker_id, action="source cache cleanup claim")
    return service.claim_source_cache_cleanup(request, principal=principal)


@router.post(
    "/worker-repository/complete-source-cache-cleanup",
    response_model=ResolveSourceCacheCleanupResponse,
)
def complete_source_cache_cleanup(
    request: ResolveSourceCacheCleanupRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> ResolveSourceCacheCleanupResponse:
    _require_worker_subject(principal, request.worker_id, action="source cache cleanup completion")
    return service.complete_source_cache_cleanup(request, principal=principal)


@router.post(
    "/worker-repository/fail-source-cache-cleanup",
    response_model=ResolveSourceCacheCleanupResponse,
)
def fail_source_cache_cleanup(
    request: ResolveSourceCacheCleanupRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> ResolveSourceCacheCleanupResponse:
    _require_worker_subject(principal, request.worker_id, action="source cache cleanup failure")
    return service.fail_source_cache_cleanup(request, principal=principal)


@router.post(
    "/worker-repository/update-container-status",
    response_model=UpdateContainerStatusResponse,
)
def update_container_status(
    request: UpdateContainerStatusRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> UpdateContainerStatusResponse:
    return service.update_container_status(request, principal=principal)


@router.post(
    "/worker-repository/set-container-exit-code",
    response_model=SetContainerExitCodeResponse,
)
def set_container_exit_code(
    request: SetContainerExitCodeRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> SetContainerExitCodeResponse:
    return service.set_container_exit_code(request, principal=principal)


@router.post(
    "/worker-repository/get-container-state",
    response_model=GetContainerStateResponse,
)
def get_container_state(
    request: GetContainerStateRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetContainerStateResponse:
    return service.get_container_state(request, principal=principal)


@router.post(
    "/worker-repository/delete-container-state",
    response_model=DeleteContainerStateResponse,
)
def delete_container_state(
    request: DeleteContainerStateRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> DeleteContainerStateResponse:
    return service.delete_container_state(request, principal=principal)


@router.post(
    "/worker-repository/set-worker-address",
    response_model=SetWorkerAddressResponse,
)
def set_worker_address(
    request: SetWorkerAddressRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> SetWorkerAddressResponse:
    return service.set_worker_address(request, principal=principal)


@router.post(
    "/worker-repository/set-container-address",
    response_model=SetContainerAddressResponse,
)
def set_container_address(
    request: SetContainerAddressRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> SetContainerAddressResponse:
    _ = principal
    return service.set_container_address(request)


@router.post(
    "/worker-repository/set-container-address-map",
    response_model=SetContainerAddressMapResponse,
)
def set_container_address_map(
    request: SetContainerAddressMapRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> SetContainerAddressMapResponse:
    _ = principal
    return service.set_container_address_map(request)


@router.post(
    "/worker-repository/get-container-address",
    response_model=GetContainerAddressResponse,
)
def get_container_address(
    request: GetContainerAddressRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetContainerAddressResponse:
    _ = principal
    return service.get_container_address(request)


@router.post(
    "/worker-repository/get-worker-address",
    response_model=GetWorkerAddressResponse,
)
def get_worker_address(
    request: GetWorkerAddressRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetWorkerAddressResponse:
    _ = principal
    return service.get_worker_address(request)


@router.post(
    "/worker-repository/get-container-address-map",
    response_model=GetContainerAddressMapResponse,
)
def get_container_address_map(
    request: GetContainerAddressMapRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetContainerAddressMapResponse:
    _ = principal
    return service.get_container_address_map(request)


@router.post(
    "/worker-repository/get-cache-origin-credentials",
    response_model=GetCacheOriginCredentialsResponse,
)
def get_cache_origin_credentials(
    request: CacheOriginCredentialRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetCacheOriginCredentialsResponse:
    return service.get_cache_origin_credentials(request, principal=principal)


@router.post(
    "/worker-repository/get-image-archive-upload-credentials",
    response_model=GetImageArchiveUploadCredentialsResponse,
)
def get_image_archive_upload_credentials(
    request: ImageArchiveUploadCredentialRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetImageArchiveUploadCredentialsResponse:
    return service.get_image_archive_upload_credentials(request, principal=principal)


@router.post(
    "/worker-repository/get-image-build-credentials",
    response_model=GetImageBuildCredentialsResponse,
)
def get_image_build_credentials(
    request: GetImageBuildCredentialsRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetImageBuildCredentialsResponse:
    return service.get_image_build_credentials(request, principal=principal)


@router.post(
    "/worker-repository/prepare-image-build-context-download",
    response_model=PrepareImageBuildContextDownloadResponse,
)
def prepare_image_build_context_download(
    request: PrepareImageBuildContextDownloadRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> PrepareImageBuildContextDownloadResponse:
    return service.prepare_image_build_context_download(request, principal=principal)


@router.post(
    "/worker-repository/get-container-credentials",
    response_model=GetContainerCredentialsResponse,
)
def get_container_credentials(
    request: ContainerCredentialRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetContainerCredentialsResponse:
    return service.get_container_credentials(request, principal=principal)


@router.post(
    "/worker-repository/save-checkpoint-state",
    response_model=SaveCheckpointStateResponse,
)
def save_checkpoint_state(
    request: SaveCheckpointStateRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> SaveCheckpointStateResponse:
    _ = principal
    return service.save_checkpoint_state(request)


@router.post(
    "/worker-repository/acquire-automatic-checkpoint-lease",
    response_model=AcquireAutomaticCheckpointLeaseResponse,
)
def acquire_automatic_checkpoint_lease(
    request: AcquireAutomaticCheckpointLeaseRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> AcquireAutomaticCheckpointLeaseResponse:
    return service.acquire_automatic_checkpoint_lease(request, principal=principal)


@router.post(
    "/worker-repository/release-automatic-checkpoint-lease",
    response_model=ReleaseAutomaticCheckpointLeaseResponse,
)
def release_automatic_checkpoint_lease(
    request: ReleaseAutomaticCheckpointLeaseRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> ReleaseAutomaticCheckpointLeaseResponse:
    return service.release_automatic_checkpoint_lease(request, principal=principal)


@router.post(
    "/worker-repository/get-checkpoint-restore",
    response_model=GetCheckpointRestoreResponse,
)
def get_checkpoint_restore(
    request: GetCheckpointRestoreRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetCheckpointRestoreResponse:
    _ = principal
    return service.get_checkpoint_restore(request)


@router.post(
    "/worker-repository/persist-checkpoint-archive",
    response_model=PersistCheckpointArchiveResponse,
)
def persist_checkpoint_archive(
    request: PersistCheckpointArchiveRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> PersistCheckpointArchiveResponse:
    _ = principal
    return service.persist_checkpoint_archive(request)


@router.post(
    "/worker-repository/prepare-checkpoint-archive-upload",
    response_model=PrepareCheckpointArchiveUploadResponse,
)
def prepare_checkpoint_archive_upload(
    request: PrepareCheckpointArchiveUploadRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> PrepareCheckpointArchiveUploadResponse:
    _ = principal
    return service.prepare_checkpoint_archive_upload(request)


@router.post(
    "/worker-repository/publish-worker-event",
    response_model=PublishWorkerEventResponse,
)
def publish_worker_event(
    request: PublishWorkerEventRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> PublishWorkerEventResponse:
    _ = principal
    return service.publish_worker_event(request)


@router.post(
    "/worker-repository/record-worker-usage",
    response_model=RecordWorkerUsageResponse,
)
def record_worker_usage(
    request: RecordWorkerUsageRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> RecordWorkerUsageResponse:
    return service.record_worker_usage(request, worker_id=principal.worker_id)


@router.post(
    "/worker-repository/publish-container-lifecycle",
    response_model=PublishContainerLifecycleResponse,
)
def publish_container_lifecycle(
    request: PublishContainerLifecycleRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> PublishContainerLifecycleResponse:
    _ = principal
    return service.publish_container_lifecycle(request)


@router.post(
    "/worker-repository/publish-container-metrics",
    response_model=PublishContainerMetricsResponse,
)
def publish_container_metrics(
    request: PublishContainerMetricsRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> PublishContainerMetricsResponse:
    _ = principal
    return service.publish_container_metrics(request)


@router.post(
    "/worker-repository/publish-container-event",
    response_model=PublishContainerEventResponse,
)
def publish_container_event(
    request: PublishContainerEventRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> PublishContainerEventResponse:
    _ = principal
    return service.publish_container_event(request)


@router.post(
    "/worker-repository/append-sandbox-process-log",
    response_model=AppendSandboxProcessLogResponse,
)
def append_sandbox_process_log(
    request: AppendSandboxProcessLogRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> AppendSandboxProcessLogResponse:
    _ = principal
    return service.append_sandbox_process_log(request)


@router.post(
    "/worker-repository/append-container-logs",
    response_model=AppendContainerLogsResponse,
    operation_id="append_container_logs",
)
def append_container_logs(
    request: AppendContainerLogsRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> AppendContainerLogsResponse:
    return service.append_container_logs(request, principal=principal)


@router.post("/worker-repository/set-network-lock", response_model=NetworkLockResponse)
def set_network_lock(
    request: NetworkLockRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> NetworkLockResponse:
    return service.set_network_lock(request, principal=principal)


@router.post(
    "/worker-repository/remove-network-lock",
    response_model=RemoveNetworkLockResponse,
)
def remove_network_lock(
    request: RemoveNetworkLockRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> RemoveNetworkLockResponse:
    return service.remove_network_lock(request, principal=principal)


@router.post("/worker-repository/set-container-ip", response_model=SetContainerIpResponse)
def set_container_ip(
    request: SetContainerIpRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> SetContainerIpResponse:
    return service.set_container_ip(request, principal=principal)


@router.post("/worker-repository/move-container-ip", response_model=MoveContainerIpResponse)
def move_container_ip(
    request: MoveContainerIpRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> MoveContainerIpResponse:
    return service.move_container_ip(request, principal=principal)


@router.post("/worker-repository/get-container-ip", response_model=GetContainerIpResponse)
def get_container_ip(
    request: GetContainerIpRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetContainerIpResponse:
    return service.get_container_ip(request, principal=principal)


@router.post("/worker-repository/get-container-ips", response_model=GetContainerIpsResponse)
def get_container_ips(
    request: GetContainerIpsRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetContainerIpsResponse:
    return service.get_container_ips(request, principal=principal)


@router.post(
    "/worker-repository/get-container-ip-assignments",
    response_model=GetContainerIpAssignmentsResponse,
)
def get_container_ip_assignments(
    request: GetContainerIpAssignmentsRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> GetContainerIpAssignmentsResponse:
    return service.get_container_ip_assignments(request, principal=principal)


@router.post(
    "/worker-repository/remove-container-ip",
    response_model=RemoveContainerIpResponse,
)
def remove_container_ip(
    request: RemoveContainerIpRequest,
    service: WorkerRepo,
    principal: WorkerPrincipal,
) -> RemoveContainerIpResponse:
    return service.remove_container_ip(request, principal=principal)
