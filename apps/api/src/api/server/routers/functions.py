from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from control.service import ControlPlaneService, StubKind, StubRecord
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from shared.function_payloads import FunctionJsonInvocation
from shared.http.functions import (
    FunctionCronRequest,
    FunctionCronResponse,
    FunctionGetArgsRequest,
    FunctionGetArgsResponse,
    FunctionInvokeBody,
    FunctionInvokeResponse,
    FunctionMonitorRequest,
    FunctionMonitorResponse,
    FunctionSetResultBody,
    FunctionSetResultResponse,
)
from shared.http.task_payload import serialize_http_task_payload

from api.server.auth import read_app_token, read_workspace, write_token, write_workspace
from api.server.dependencies import current_services
from api.server.deployed_stubs import (
    resolve_deployed_stub,
    resolve_deployed_stub_id,
)
from api.server.http import request_query_params
from api.server.ownership import require_function_stub_workspace, require_task_workspace
from api.server.service_dependencies import control_plane_service, function_service
from api.server.services import ApiServices, FunctionApiService

router = APIRouter(prefix="/api/v1/functions", tags=["function"])


@router.post("/invoke", response_model=FunctionInvokeResponse)
def function_invoke(
    request: FunctionInvokeBody,
    token: write_token,
    workspace_id: write_workspace,
    service: FunctionApiService = Depends(function_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
) -> FunctionInvokeResponse:
    require_function_stub_workspace(control_plane, request.stub_id, workspace_id)
    return service.function_invoke(request)


@router.post("/invoke/stream", response_class=StreamingResponse)
def function_invoke_stream(
    request: FunctionInvokeBody,
    token: write_token,
    workspace_id: write_workspace,
    service: FunctionApiService = Depends(function_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
) -> StreamingResponse:
    require_function_stub_workspace(control_plane, request.stub_id, workspace_id)
    return StreamingResponse(
        _function_ndjson(service.function_invoke_stream(request)),
        media_type="application/x-ndjson",
    )


@router.post("/get-args", response_model=FunctionGetArgsResponse)
def function_get_args(
    request: FunctionGetArgsRequest,
    token: read_app_token,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
    service: FunctionApiService = Depends(function_service),
) -> FunctionGetArgsResponse:
    require_task_workspace(services, request.task_id, workspace_id)
    return service.function_get_args(request)


@router.post("/set-result", response_model=FunctionSetResultResponse)
def function_set_result(
    request: FunctionSetResultBody,
    token: write_token,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
    service: FunctionApiService = Depends(function_service),
) -> FunctionSetResultResponse:
    require_task_workspace(services, request.task_id, workspace_id)
    return service.function_set_result(request)


@router.post("/monitor", response_model=FunctionMonitorResponse)
def function_monitor(
    request: FunctionMonitorRequest,
    token: read_app_token,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    service: FunctionApiService = Depends(function_service),
) -> FunctionMonitorResponse:
    require_function_stub_workspace(control_plane, request.stub_id, workspace_id)
    require_task_workspace(services, request.task_id, workspace_id)
    return service.function_monitor(request)


@router.post("/cron", response_model=FunctionCronResponse)
def function_cron(
    request: FunctionCronRequest,
    token: write_token,
    workspace_id: write_workspace,
    control_plane: ControlPlaneService = Depends(control_plane_service),
    service: FunctionApiService = Depends(function_service),
) -> FunctionCronResponse:
    require_function_stub_workspace(control_plane, request.stub_id, workspace_id)
    return service.function_cron(request)


@router.post("/id/{stub_id}", response_model=FunctionInvokeResponse)
async def deployed_function_invoke_by_id(
    stub_id: str,
    request: Request,
    token: write_token,
    workspace_id: write_workspace,
    service: FunctionApiService = Depends(function_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> FunctionInvokeResponse:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.Function,
        public=False,
        resource_name="function",
        workspace=workspace_id,
    )
    return await _invoke_deployed_function(stub, request, service)


@router.post("/public/{stub_id}", response_model=FunctionInvokeResponse)
async def deployed_public_function_invoke_by_id(
    stub_id: str,
    request: Request,
    service: FunctionApiService = Depends(function_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> FunctionInvokeResponse:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.Function,
        public=True,
        resource_name="function",
    )
    return await _invoke_deployed_function(stub, request, service)


@router.post("/{deployment_name}/latest", response_model=FunctionInvokeResponse)
async def deployed_function_invoke_by_latest_path(
    deployment_name: str,
    request: Request,
    token: write_token,
    workspace_id: write_workspace,
    service: FunctionApiService = Depends(function_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> FunctionInvokeResponse:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Function,
        version=None,
        workspace=workspace_id,
        resource_name="function",
    )
    return await _invoke_deployed_function(stub, request, service)


@router.post("/{deployment_name}/v{version}", response_model=FunctionInvokeResponse)
async def deployed_function_invoke_by_version(
    deployment_name: str,
    version: int,
    request: Request,
    token: write_token,
    workspace_id: write_workspace,
    service: FunctionApiService = Depends(function_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> FunctionInvokeResponse:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Function,
        version=version,
        workspace=workspace_id,
        resource_name="function",
    )
    return await _invoke_deployed_function(stub, request, service)


async def _invoke_deployed_function(
    stub: StubRecord,
    request: Request,
    service: FunctionApiService,
) -> FunctionInvokeResponse:
    return service.function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=await _http_invocation(request),
        )
    )


async def _http_invocation(request: Request) -> FunctionJsonInvocation:
    body = await request.body()
    payload = serialize_http_task_payload(body, query_params=request_query_params(request))
    return FunctionJsonInvocation(
        args=list(payload.args or []),
        kwargs=dict(payload.kwargs),
    )


def _function_ndjson(items: Iterable[FunctionInvokeResponse]) -> Iterator[bytes]:
    for item in items:
        yield (json.dumps(item.model_dump(mode="json"), separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
