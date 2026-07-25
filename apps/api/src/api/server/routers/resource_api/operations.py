from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from scheduler.autoscaler_operations import AutoscalerOperationsService
from scheduler.service import Scheduler, SchedulerWorkloadControls
from shared.autoscaler_state import AutoscalerTargetKind
from shared.http.operations import (
    AgentLeaseListResponse,
    AgentLeaseRequest,
    AgentLeaseResponse,
    AgentListResponse,
    AgentRegisterRequest,
    AgentResponse,
    AutoscalerControlResponse,
    AutoscalerHistoryResponse,
    AutoscalerReconcileResponse,
    AutoscalerStatusItemResponse,
    AutoscalerStatusListResponse,
    CronJobListResponse,
    CronJobResponse,
    CronJobRunListResponse,
    CronJobRunResponse,
    ImageBuildListResponse,
    ImageBuildRequest,
    ImageBuildResponse,
    ProviderListResponse,
    ProviderResponse,
    ProviderSetRequest,
    SchedulerContainerDispatchListResponse,
    SchedulerContainerDispatchResponse,
)
from shared.http.storage import (
    CacheContentResponse,
    CacheCreateRequest,
    CacheEntryListResponse,
    CacheEntryResponse,
    ObjectContentResponse,
    ObjectCreateRequest,
    ObjectListResponse,
    ObjectResponse,
)
from shared.identity import AuthScope

from api.server.auth import (
    admin_access,
    read_token,
    read_workspace,
    write_token,
    write_workspace,
)
from api.server.dependencies import (
    authorize_token_workspace,
    current_services,
)
from api.server.service_dependencies import autoscaler_operations_service
from api.server.services import ApiServices

router = APIRouter()


@router.get("/api/v1/cron-jobs", response_model=CronJobListResponse, operation_id="list_cron_jobs")
def list_cron_jobs(
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> CronJobListResponse:
    return CronJobListResponse(
        cron_jobs=[
            CronJobResponse.model_validate(item)
            for item in services.cron_jobs.list(workspace=workspace_id)
        ]
    )


@router.delete(
    "/api/v1/cron-jobs/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_cron_job",
)
def delete_cron_job(
    name: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> None:
    services.cron_jobs.delete(name, workspace=workspace_id)


@router.get(
    "/api/v1/cron-job-runs",
    response_model=CronJobRunListResponse,
    operation_id="list_cron_job_runs",
)
def list_cron_job_runs(
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> CronJobRunListResponse:
    return CronJobRunListResponse(
        runs=[
            CronJobRunResponse.model_validate(item)
            for item in Scheduler(services).list_cron_job_runs()
        ]
    )


@router.post(
    "/api/v1/scheduler/tick",
    response_model=CronJobRunListResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="tick_scheduler",
)
def tick_scheduler(
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> CronJobRunListResponse:
    return CronJobRunListResponse(
        runs=[CronJobRunResponse.model_validate(item) for item in Scheduler(services).tick()]
    )


@router.post(
    "/api/v1/scheduler/containers/dispatch",
    response_model=SchedulerContainerDispatchListResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="dispatch_scheduler_containers",
)
def dispatch_scheduler_containers(
    limit: int = 100,
    *,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> SchedulerContainerDispatchListResponse:
    # Inject the container request service the deployment already builds. Without
    # it this route raised "scheduler container request service was not injected",
    # so the one command that reports why a request will not place was unusable
    # exactly when it was needed.
    scheduler = Scheduler(
        services,
        workloads=SchedulerWorkloadControls(containers=services.scheduler_container_requests),
    )
    return SchedulerContainerDispatchListResponse(
        dispatches=[
            SchedulerContainerDispatchResponse.model_validate(item)
            for item in scheduler.dispatch_containers(limit=limit)
        ]
    )


@router.get(
    "/api/v1/scheduler/autoscalers",
    response_model=AutoscalerStatusListResponse,
    operation_id="list_autoscalers",
)
def list_autoscalers(
    workspace: str | None = None,
    target_kind: AutoscalerTargetKind | None = None,
    target_id: str | None = None,
    *,
    token: read_token,
    services: ApiServices = Depends(current_services),
    service: AutoscalerOperationsService = Depends(autoscaler_operations_service),
) -> AutoscalerStatusListResponse:
    workspace_id = (
        token.workspace_id
        if workspace is None
        else authorize_token_workspace(services, token, workspace, AuthScope.Read)
    )
    result = service.status(
        workspace=workspace_id,
        target_kind=target_kind,
        target_id=target_id,
    )
    return AutoscalerStatusListResponse(
        items=[AutoscalerStatusItemResponse.model_validate(item) for item in result.items]
    )


@router.get(
    "/api/v1/scheduler/autoscalers/history",
    response_model=AutoscalerHistoryResponse,
    operation_id="list_autoscaler_history",
)
def list_autoscaler_history(
    workspace: str | None = None,
    target_id: str | None = None,
    limit: int = 100,
    *,
    token: read_token,
    services: ApiServices = Depends(current_services),
    service: AutoscalerOperationsService = Depends(autoscaler_operations_service),
) -> AutoscalerHistoryResponse:
    workspace_id = (
        token.workspace_id
        if workspace is None
        else authorize_token_workspace(services, token, workspace, AuthScope.Read)
    )
    return AutoscalerHistoryResponse.model_validate(
        service.history(workspace=workspace_id, target_id=target_id, limit=limit)
    )


@router.post(
    "/api/v1/scheduler/autoscalers/reconcile",
    response_model=AutoscalerReconcileResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="reconcile_autoscalers",
)
def reconcile_autoscalers(
    workspace: str = "default",
    target_kind: AutoscalerTargetKind | None = None,
    stub_id: str | None = None,
    *,
    token: write_token,
    services: ApiServices = Depends(current_services),
    service: AutoscalerOperationsService = Depends(autoscaler_operations_service),
) -> AutoscalerReconcileResponse:
    workspace_id = authorize_token_workspace(services, token, workspace, AuthScope.Write)
    result = service.reconcile(
        workspace=workspace_id,
        target_kind=target_kind,
        stub_id_or_name=stub_id,
    )
    return AutoscalerReconcileResponse(results=list(result.results))


@router.post(
    "/api/v1/scheduler/autoscalers/{stub_id_or_name}/pause",
    response_model=AutoscalerControlResponse,
    operation_id="pause_autoscaler",
)
def pause_autoscaler(
    stub_id_or_name: str,
    workspace: str = "default",
    *,
    token: write_token,
    services: ApiServices = Depends(current_services),
    service: AutoscalerOperationsService = Depends(autoscaler_operations_service),
) -> AutoscalerControlResponse:
    workspace_id = authorize_token_workspace(services, token, workspace, AuthScope.Write)
    return AutoscalerControlResponse.model_validate(
        service.pause(stub_id_or_name, workspace=workspace_id)
    )


@router.post(
    "/api/v1/scheduler/autoscalers/{stub_id_or_name}/resume",
    response_model=AutoscalerControlResponse,
    operation_id="resume_autoscaler",
)
def resume_autoscaler(
    stub_id_or_name: str,
    workspace: str = "default",
    *,
    token: write_token,
    services: ApiServices = Depends(current_services),
    service: AutoscalerOperationsService = Depends(autoscaler_operations_service),
) -> AutoscalerControlResponse:
    workspace_id = authorize_token_workspace(services, token, workspace, AuthScope.Write)
    return AutoscalerControlResponse.model_validate(
        service.resume(stub_id_or_name, workspace=workspace_id)
    )


@router.get(
    "/api/v1/image-builds",
    response_model=ImageBuildListResponse,
    operation_id="list_image_builds",
)
def list_image_builds(
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> ImageBuildListResponse:
    return ImageBuildListResponse(
        builds=[
            ImageBuildResponse.model_validate(item)
            for item in services.images.list_for_workspace(workspace_id=workspace_id)
        ]
    )


@router.get(
    "/api/v1/image-builds/{build_id}",
    response_model=ImageBuildResponse,
    operation_id="get_image_build",
)
def get_image_build(
    build_id: str,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> ImageBuildResponse:
    return ImageBuildResponse.model_validate(
        services.images.get_for_workspace(build_id, workspace_id=workspace_id)
    )


@router.post(
    "/api/v1/image-builds",
    response_model=ImageBuildResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_image_build",
)
def create_image_build(
    request: ImageBuildRequest,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> ImageBuildResponse:
    return ImageBuildResponse.model_validate(
        services.images.build(
            request.image,
            workspace_id=workspace_id,
            tag=request.tag,
        )
    )


@router.post(
    "/api/v1/image-builds/{build_id}/cancel",
    response_model=ImageBuildResponse,
    operation_id="cancel_image_build",
)
def cancel_image_build(
    build_id: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> ImageBuildResponse:
    return ImageBuildResponse.model_validate(
        services.images.cancel_for_workspace(build_id, workspace_id=workspace_id)
    )


@router.get("/api/v1/objects", response_model=ObjectListResponse, operation_id="list_objects")
def list_objects(
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
    bucket: str | None = None,
    prefix: str = "",
) -> ObjectListResponse:
    return ObjectListResponse(
        objects=[
            ObjectResponse.model_validate(item)
            for item in services.object_storage.list_for_workspace(
                workspace_id=workspace_id,
                bucket=bucket,
                prefix=prefix,
            )
        ]
    )


@router.post(
    "/api/v1/objects",
    response_model=ObjectResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_object",
)
def create_object(
    request: ObjectCreateRequest,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> ObjectResponse:
    return ObjectResponse.model_validate(
        services.object_storage.put_bytes_for_workspace(
            workspace_id=workspace_id,
            bucket=request.bucket,
            key=request.key,
            data=request.bytes_value(),
            content_type=request.content_type,
            metadata=request.metadata,
        )
    )


@router.get(
    "/api/v1/objects/{bucket}/{key:path}",
    response_model=ObjectContentResponse,
    operation_id="read_object",
)
def read_object(
    bucket: str,
    key: str,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> ObjectContentResponse:
    record, data = services.object_storage.read_content_for_workspace(
        workspace_id=workspace_id,
        bucket=bucket,
        key=key,
    )
    return ObjectContentResponse.from_content(
        record=ObjectResponse.model_validate(record),
        data=data,
    )


@router.delete(
    "/api/v1/objects/{bucket}/{key:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_object",
)
def delete_object(
    bucket: str,
    key: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> None:
    services.object_storage.delete_required_for_workspace(
        workspace_id=workspace_id,
        bucket=bucket,
        key=key,
    )


@router.get("/api/v1/cache", response_model=CacheEntryListResponse, operation_id="list_cache")
def list_cache(
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> CacheEntryListResponse:
    return CacheEntryListResponse(
        entries=[CacheEntryResponse.model_validate(item) for item in services.cache_storage.list()]
    )


@router.post(
    "/api/v1/cache",
    response_model=CacheEntryResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_cache_entry",
)
def create_cache_entry(
    request: CacheCreateRequest,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> CacheEntryResponse:
    return CacheEntryResponse.model_validate(
        services.cache_storage.put_bytes(request.namespace, request.key, request.bytes_value())
    )


@router.get(
    "/api/v1/cache/{namespace}/{key:path}",
    response_model=CacheContentResponse,
    operation_id="read_cache_entry",
)
def read_cache_entry(
    namespace: str,
    key: str,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> CacheContentResponse:
    entry, data = services.cache_storage.get_bytes(namespace, key)
    return CacheContentResponse.from_content(
        entry=CacheEntryResponse.model_validate(entry),
        data=data,
    )


@router.delete(
    "/api/v1/cache/{namespace}/{key:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_cache_entry",
)
def delete_cache_entry(
    namespace: str,
    key: str,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> None:
    services.cache_storage.delete(namespace, key)


@router.get("/api/v1/providers", response_model=ProviderListResponse, operation_id="list_providers")
def list_providers(
    workspace_id: read_workspace,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> ProviderListResponse:
    return ProviderListResponse(
        providers=[
            ProviderResponse.model_validate(item)
            for item in services.providers.list(enabled=None, workspace=workspace_id)
        ]
    )


@router.post(
    "/api/v1/providers",
    response_model=ProviderResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="set_provider",
)
def set_provider(
    request: ProviderSetRequest,
    workspace_id: write_workspace,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> ProviderResponse:
    return ProviderResponse.model_validate(
        services.providers.set(
            request.name,
            kind=request.kind,
            enabled=request.enabled,
            priority=request.priority,
            config=request.config,
            labels=request.labels,
            workspace=workspace_id,
        )
    )


@router.delete(
    "/api/v1/providers/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_provider",
)
def delete_provider(
    name: str,
    workspace_id: write_workspace,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> None:
    services.providers.delete(name, workspace=workspace_id)


@router.get("/api/v1/agents", response_model=AgentListResponse, operation_id="list_agents")
def list_agents(
    workspace_id: read_workspace,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> AgentListResponse:
    return AgentListResponse(
        agents=[
            AgentResponse.model_validate(item)
            for item in services.agents.list_agents(workspace=workspace_id)
        ]
    )


@router.post(
    "/api/v1/agents",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="register_agent",
)
def register_agent(
    request: AgentRegisterRequest,
    workspace_id: write_workspace,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> AgentResponse:
    return AgentResponse.model_validate(
        services.agents.register(
            request.name,
            pool=request.pool,
            version=request.version,
            capacity=dict(request.capacity),
            labels=request.labels,
            workspace=workspace_id,
        )
    )


@router.post(
    "/api/v1/agents/{agent_id}/heartbeat",
    response_model=AgentResponse,
    operation_id="heartbeat_agent",
)
def heartbeat_agent(
    agent_id: str,
    workspace_id: write_workspace,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> AgentResponse:
    return AgentResponse.model_validate(services.agents.heartbeat(agent_id, workspace=workspace_id))


@router.post(
    "/api/v1/agents/{agent_id}/leases",
    response_model=AgentLeaseResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="lease_agent",
)
def lease_agent(
    agent_id: str,
    request: AgentLeaseRequest,
    workspace_id: write_workspace,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> AgentLeaseResponse:
    return AgentLeaseResponse.model_validate(
        services.agents.lease(
            agent_id,
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            ttl_seconds=request.ttl_seconds,
            workspace=workspace_id,
        )
    )


@router.delete(
    "/api/v1/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_agent",
)
def delete_agent(
    agent_id: str,
    workspace_id: write_workspace,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> None:
    services.agents.delete(agent_id, workspace=workspace_id)


@router.get("/api/v1/leases", response_model=AgentLeaseListResponse, operation_id="list_leases")
def list_leases(
    include_inactive: bool = False,
    *,
    workspace_id: read_workspace,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> AgentLeaseListResponse:
    return AgentLeaseListResponse(
        leases=[
            AgentLeaseResponse.model_validate(item)
            for item in services.agents.list_leases(
                workspace=workspace_id,
                include_inactive=include_inactive,
            )
        ]
    )


@router.post(
    "/api/v1/leases/{lease_id}/release",
    response_model=AgentLeaseResponse,
    operation_id="release_lease",
)
def release_lease(
    lease_id: str,
    workspace_id: write_workspace,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> AgentLeaseResponse:
    return AgentLeaseResponse.model_validate(
        services.agents.release(lease_id, workspace=workspace_id)
    )
