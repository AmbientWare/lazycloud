from __future__ import annotations

import pickle

from control.service import ControlPlaneService, StubKind, StubRecord
from fastapi import APIRouter, Depends, Request
from shared.http.task_payload import serialize_http_task_payload
from shared.http.taskqueues import (
    StartTaskQueueServeRequest,
    StartTaskQueueServeResponse,
    TaskQueueCompleteBody,
    TaskQueueCompleteResponse,
    TaskQueueInvocationEnvelope,
    TaskQueueMonitorRequest,
    TaskQueueMonitorResponse,
    TaskQueuePopRequest,
    TaskQueuePopResponse,
    TaskQueuePutBody,
    TaskQueuePutResponse,
    TaskQueueStateResponse,
)

from api.server.auth import read_workspace, write_workspace
from api.server.dependencies import current_services
from api.server.deployed_stubs import (
    resolve_deployed_stub,
    resolve_deployed_stub_id,
)
from api.server.http import request_query_params
from api.server.ownership import require_task_queue_stub_workspace, require_task_workspace
from api.server.service_dependencies import control_plane_service, taskqueue_service
from api.server.services import ApiServices, TaskQueueApiService

router = APIRouter(prefix="/api/v1/taskqueues", tags=["taskqueue"])


@router.post("/put", response_model=TaskQueuePutResponse)
def task_queue_put(
    request: TaskQueuePutBody,
    workspace_id: write_workspace,
    service: TaskQueueApiService = Depends(taskqueue_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
) -> TaskQueuePutResponse:
    require_task_queue_stub_workspace(control_plane, request.stub_id, workspace_id)
    return service.task_queue_put(request.stub_id, request.invocation.bytes_value())


@router.post("/pop", response_model=TaskQueuePopResponse)
def task_queue_pop(
    request: TaskQueuePopRequest,
    workspace_id: write_workspace,
    control_plane: ControlPlaneService = Depends(control_plane_service),
    service: TaskQueueApiService = Depends(taskqueue_service),
) -> TaskQueuePopResponse:
    require_task_queue_stub_workspace(control_plane, request.stub_id, workspace_id)
    return service.task_queue_pop(request)


@router.post("/monitor", response_model=TaskQueueMonitorResponse)
def task_queue_monitor(
    request: TaskQueueMonitorRequest,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    service: TaskQueueApiService = Depends(taskqueue_service),
) -> TaskQueueMonitorResponse:
    require_task_queue_stub_workspace(control_plane, request.stub_id, workspace_id)
    require_task_workspace(services, request.task_id, workspace_id)
    return service.task_queue_monitor(request)


@router.post("/complete", response_model=TaskQueueCompleteResponse)
def task_queue_complete(
    request: TaskQueueCompleteBody,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    service: TaskQueueApiService = Depends(taskqueue_service),
) -> TaskQueueCompleteResponse:
    require_task_queue_stub_workspace(control_plane, request.stub_id, workspace_id)
    require_task_workspace(services, request.task_id, workspace_id)
    return service.task_queue_complete(request)


@router.get(
    "/{stub_id}/state",
    response_model=TaskQueueStateResponse,
    operation_id="get_task_queue_state",
)
def task_queue_state(
    stub_id: str,
    workspace_id: read_workspace,
    control_plane: ControlPlaneService = Depends(control_plane_service),
    service: TaskQueueApiService = Depends(taskqueue_service),
) -> TaskQueueStateResponse:
    require_task_queue_stub_workspace(control_plane, stub_id, workspace_id)
    return service.task_queue_state(stub_id)


@router.post("/serve", response_model=StartTaskQueueServeResponse)
def start_task_queue_serve(
    request: StartTaskQueueServeRequest,
    workspace_id: write_workspace,
    control_plane: ControlPlaneService = Depends(control_plane_service),
    service: TaskQueueApiService = Depends(taskqueue_service),
) -> StartTaskQueueServeResponse:
    require_task_queue_stub_workspace(control_plane, request.stub_id, workspace_id)
    return service.start_task_queue_serve(request)


@router.post("/id/{stub_id}", response_model=TaskQueuePutResponse)
async def deployed_task_queue_put_by_id(
    stub_id: str,
    request: Request,
    workspace_id: write_workspace,
    service: TaskQueueApiService = Depends(taskqueue_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> TaskQueuePutResponse:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.TaskQueue,
        public=False,
        resource_name="task queue",
        workspace=workspace_id,
    )
    return await _put_deployed_task_queue(stub, request, service)


@router.post("/public/{stub_id}", response_model=TaskQueuePutResponse)
async def deployed_public_task_queue_put_by_id(
    stub_id: str,
    request: Request,
    service: TaskQueueApiService = Depends(taskqueue_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> TaskQueuePutResponse:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.TaskQueue,
        public=True,
        resource_name="task queue",
    )
    return await _put_deployed_task_queue(stub, request, service)


@router.post("/id/{stub_id}/warmup", response_model=StartTaskQueueServeResponse)
def deployed_task_queue_warmup_by_id(
    stub_id: str,
    workspace_id: write_workspace,
    service: TaskQueueApiService = Depends(taskqueue_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> StartTaskQueueServeResponse:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.TaskQueue,
        public=False,
        resource_name="task queue",
        workspace=workspace_id,
    )
    return _warm_deployed_task_queue(stub, service)


@router.post("/{deployment_name}/latest/warmup", response_model=StartTaskQueueServeResponse)
def deployed_task_queue_warmup_by_latest_path(
    deployment_name: str,
    workspace_id: write_workspace,
    service: TaskQueueApiService = Depends(taskqueue_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> StartTaskQueueServeResponse:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.TaskQueue,
        version=None,
        workspace=workspace_id,
        resource_name="task queue",
    )
    return _warm_deployed_task_queue(stub, service)


@router.post("/{deployment_name}/v{version}/warmup", response_model=StartTaskQueueServeResponse)
def deployed_task_queue_warmup_by_version(
    deployment_name: str,
    version: int,
    workspace_id: write_workspace,
    service: TaskQueueApiService = Depends(taskqueue_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> StartTaskQueueServeResponse:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.TaskQueue,
        version=version,
        workspace=workspace_id,
        resource_name="task queue",
    )
    return _warm_deployed_task_queue(stub, service)


@router.post("/{deployment_name}/latest", response_model=TaskQueuePutResponse)
async def deployed_task_queue_put_by_latest_path(
    deployment_name: str,
    request: Request,
    workspace_id: write_workspace,
    service: TaskQueueApiService = Depends(taskqueue_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> TaskQueuePutResponse:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.TaskQueue,
        version=None,
        workspace=workspace_id,
        resource_name="task queue",
    )
    return await _put_deployed_task_queue(stub, request, service)


@router.post("/{deployment_name}/v{version}", response_model=TaskQueuePutResponse)
async def deployed_task_queue_put_by_version(
    deployment_name: str,
    version: int,
    request: Request,
    workspace_id: write_workspace,
    service: TaskQueueApiService = Depends(taskqueue_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> TaskQueuePutResponse:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.TaskQueue,
        version=version,
        workspace=workspace_id,
        resource_name="task queue",
    )
    return await _put_deployed_task_queue(stub, request, service)


async def _put_deployed_task_queue(
    stub: StubRecord,
    request: Request,
    service: TaskQueueApiService,
) -> TaskQueuePutResponse:
    return service.task_queue_put(stub.id, await _encoded_task_payload(request))


def _warm_deployed_task_queue(
    stub: StubRecord,
    service: TaskQueueApiService,
) -> StartTaskQueueServeResponse:
    return service.start_task_queue_serve(StartTaskQueueServeRequest(stub_id=stub.id))


async def _encoded_task_payload(request: Request) -> bytes:
    body = await request.body()
    payload = serialize_http_task_payload(body, query_params=request_query_params(request))
    return pickle.dumps(
        TaskQueueInvocationEnvelope(
            args=tuple(payload.args or ()),
            kwargs=payload.kwargs,
        )
    )
