from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from anyio import CancelScope, to_thread
from control.service import ControlPlaneService, StubKind, StubRecord
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from shared.function_payloads import FunctionJsonInvocation
from shared.http.functions import (
    FunctionClaimRequest,
    FunctionClaimResponse,
    FunctionInvokeBody,
    FunctionInvokeResponse,
    FunctionMonitorRequest,
    FunctionMonitorResponse,
    FunctionServeRequest,
    FunctionServeResponse,
    FunctionSetResultBody,
    FunctionSetResultResponse,
)
from shared.http.task_payload import serialize_http_task_payload
from starlette.types import Receive, Scope, Send

from api.server.auth import read_workspace, write_workspace
from api.server.dependencies import current_services
from api.server.deployed_stubs import (
    resolve_deployed_stub,
    resolve_deployed_stub_id,
)
from api.server.http import request_query_params
from api.server.ownership import require_function_stub_workspace, require_task_workspace
from api.server.public_transfers import attribute_public_transfer
from api.server.service_dependencies import control_plane_service, function_service
from api.server.services import ApiServices, FunctionApiService

router = APIRouter(prefix="/api/v1/functions", tags=["function"])


class _FunctionInvokeStreamResponse(StreamingResponse):
    def __init__(
        self,
        service: FunctionApiService,
        initial: FunctionInvokeResponse,
        *,
        headless: bool,
    ) -> None:
        self._service = service
        self._task_id = (
            initial.task_id if not headless and not initial.done and initial.exit_code == 0 else ""
        )
        self._items = service.function_invoke_stream(initial, headless=headless)
        super().__init__(self._stream(), media_type="application/x-ndjson")

    async def _stream(self) -> AsyncIterator[bytes]:
        async for item in self._items:
            if item.done or item.exit_code != 0:
                self._task_id = ""
            yield (json.dumps(item.model_dump(mode="json"), separators=(",", ":")) + "\n").encode(
                "utf-8"
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Disconnect cancels the response's scope before cleanup runs.
            with CancelScope(shield=True):
                try:
                    await self._items.aclose()
                finally:
                    if self._task_id:
                        await to_thread.run_sync(self._service.cancel_task, self._task_id)


@router.post("/serve", response_model=FunctionServeResponse)
def function_serve(
    request: FunctionServeRequest,
    workspace_id: write_workspace,
    service: FunctionApiService = Depends(function_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
) -> FunctionServeResponse:
    require_function_stub_workspace(control_plane, request.stub_id, workspace_id)
    return service.start_function_serve(request)


async def _http_invocation(request: Request) -> FunctionJsonInvocation:
    body = await request.body()
    payload = serialize_http_task_payload(body, query_params=request_query_params(request))
    return FunctionJsonInvocation(
        args=list(payload.args or []),
        kwargs=dict(payload.kwargs),
    )


type HttpFunctionInvocation = Annotated[FunctionJsonInvocation, Depends(_http_invocation)]


@router.post("/invoke", response_model=FunctionInvokeResponse)
def function_invoke(
    request: FunctionInvokeBody,
    connection: Request,
    workspace_id: write_workspace,
    service: FunctionApiService = Depends(function_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
) -> FunctionInvokeResponse:
    require_function_stub_workspace(control_plane, request.stub_id, workspace_id)
    result = service.function_invoke(request)
    attribute_public_transfer(
        connection,
        workspace_id=workspace_id,
        resource_type="stub",
        resource_id=request.stub_id,
        stub_id=request.stub_id,
    )
    return result


@router.post("/invoke/stream", response_class=StreamingResponse)
def function_invoke_stream(
    request: FunctionInvokeBody,
    connection: Request,
    workspace_id: write_workspace,
    service: FunctionApiService = Depends(function_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
) -> StreamingResponse:
    require_function_stub_workspace(control_plane, request.stub_id, workspace_id)
    # Run admission before streaming starts so refusals retain their HTTP status.
    initial = service.function_invoke(request)
    attribute_public_transfer(
        connection,
        workspace_id=workspace_id,
        resource_type="stub",
        resource_id=request.stub_id,
        stub_id=request.stub_id,
    )
    return _FunctionInvokeStreamResponse(service, initial, headless=request.headless)


@router.post("/claim", response_model=FunctionClaimResponse)
def function_claim(
    request: FunctionClaimRequest,
    workspace_id: write_workspace,
    service: FunctionApiService = Depends(function_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
) -> FunctionClaimResponse:
    require_function_stub_workspace(control_plane, request.stub_id, workspace_id)
    return service.function_claim(request)


@router.post("/set-result", response_model=FunctionSetResultResponse)
def function_set_result(
    request: FunctionSetResultBody,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
    service: FunctionApiService = Depends(function_service),
) -> FunctionSetResultResponse:
    require_task_workspace(services, request.task_id, workspace_id)
    return service.function_set_result(request)


@router.post("/monitor", response_model=FunctionMonitorResponse)
def function_monitor(
    request: FunctionMonitorRequest,
    connection: Request,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    service: FunctionApiService = Depends(function_service),
) -> FunctionMonitorResponse:
    require_function_stub_workspace(control_plane, request.stub_id, workspace_id)
    require_task_workspace(services, request.task_id, workspace_id)
    result = service.function_monitor(request)
    attribute_public_transfer(
        connection,
        workspace_id=workspace_id,
        resource_type="task",
        resource_id=request.task_id,
        stub_id=request.stub_id,
    )
    return result


@router.post("/id/{stub_id}", response_model=FunctionInvokeResponse)
def deployed_function_invoke_by_id(
    stub_id: str,
    connection: Request,
    invocation: HttpFunctionInvocation,
    workspace_id: write_workspace,
    service: FunctionApiService = Depends(function_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> FunctionInvokeResponse:
    stub = resolve_deployed_stub_id(
        control_plane,
        services,
        stub_id,
        StubKind.Function,
        public=False,
        resource_name="function",
        workspace=workspace_id,
    )
    return _invoke_deployed_function(stub, invocation, service, connection)


@router.post("/public/{stub_id}", response_model=FunctionInvokeResponse)
def deployed_public_function_invoke_by_id(
    stub_id: str,
    connection: Request,
    invocation: HttpFunctionInvocation,
    service: FunctionApiService = Depends(function_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> FunctionInvokeResponse:
    stub = resolve_deployed_stub_id(
        control_plane,
        services,
        stub_id,
        StubKind.Function,
        public=True,
        resource_name="function",
    )
    return _invoke_deployed_function(stub, invocation, service, connection)


@router.post("/{deployment_name}/latest", response_model=FunctionInvokeResponse)
def deployed_function_invoke_by_latest_path(
    deployment_name: str,
    connection: Request,
    invocation: HttpFunctionInvocation,
    workspace_id: write_workspace,
    service: FunctionApiService = Depends(function_service),
    services: ApiServices = Depends(current_services),
) -> FunctionInvokeResponse:
    stub = resolve_deployed_stub(
        services,
        deployment_name,
        StubKind.Function,
        version=None,
        workspace=workspace_id,
        resource_name="function",
    )
    return _invoke_deployed_function(stub, invocation, service, connection)


@router.post("/{deployment_name}/v{version}", response_model=FunctionInvokeResponse)
def deployed_function_invoke_by_version(
    deployment_name: str,
    version: int,
    connection: Request,
    invocation: HttpFunctionInvocation,
    workspace_id: write_workspace,
    service: FunctionApiService = Depends(function_service),
    services: ApiServices = Depends(current_services),
) -> FunctionInvokeResponse:
    stub = resolve_deployed_stub(
        services,
        deployment_name,
        StubKind.Function,
        version=version,
        workspace=workspace_id,
        resource_name="function",
    )
    return _invoke_deployed_function(stub, invocation, service, connection)


def _invoke_deployed_function(
    stub: StubRecord,
    invocation: FunctionJsonInvocation,
    service: FunctionApiService,
    connection: Request,
) -> FunctionInvokeResponse:
    result = service.function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=invocation,
        )
    )
    attribute_public_transfer(
        connection,
        workspace_id=stub.workspace_id,
        resource_type="stub",
        resource_id=stub.id,
        stub_id=stub.id,
    )
    return result
