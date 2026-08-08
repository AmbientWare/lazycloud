from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from gateway.service import GatewayControlService
from shared.errors import NotFoundError
from shared.http.gateway import (
    AttachToContainerRequest,
    AttachToContainerResponse,
    CheckpointContainerRequest,
    CheckpointContainerResponse,
    SyncContainerWorkspaceBody,
    SyncContainerWorkspaceResponse,
)
from shared.http.gateway_tasks import (
    AppendTaskLogRequest,
    AppendTaskLogResponse,
    EndTaskRequest,
    EndTaskResponse,
    StartTaskRequest,
    StartTaskResponse,
)

from api.server.auth import read_workspace, write_workspace
from api.server.routers.gateway.common import gateway_write_token
from api.server.service_dependencies import gateway_service
from api.server.sse import sse_event

router = APIRouter(prefix="/gateway", tags=["gateway"])


@router.post("/containers/checkpoint", response_model=CheckpointContainerResponse)
def checkpoint_container(
    request: CheckpointContainerRequest,
    workspace_id: write_workspace,
    service: GatewayControlService = Depends(gateway_service),
) -> CheckpointContainerResponse:
    return service.checkpoint_container(request, workspace_id=workspace_id)


@router.post("/containers/attach", response_model=AttachToContainerResponse)
def attach_to_container(
    request: AttachToContainerRequest,
    workspace_id: read_workspace,
    service: GatewayControlService = Depends(gateway_service),
) -> AttachToContainerResponse:
    return service.attach_to_container(request, workspace_id=workspace_id)


@router.post("/containers/sync-workspace", response_model=SyncContainerWorkspaceResponse)
def sync_container_workspace(
    request: SyncContainerWorkspaceBody,
    workspace_id: write_workspace,
    service: GatewayControlService = Depends(gateway_service),
) -> SyncContainerWorkspaceResponse:
    return service.sync_container_workspace(request, workspace_id=workspace_id)


@router.get("/containers/attach/stream", response_class=StreamingResponse)
def attach_to_container_stream(
    container_id: str = Query(...),
    max_events: int = Query(0, ge=0),
    max_idle_polls: int = Query(0, ge=0),
    poll_interval_seconds: float = Query(0.25, ge=0.05, le=30),
    *,
    workspace_id: read_workspace,
    service: GatewayControlService = Depends(gateway_service),
) -> StreamingResponse:
    return StreamingResponse(
        _attach_events(
            service,
            container_id,
            poll_interval_seconds,
            workspace_id=workspace_id,
            max_events=max_events,
            max_idle_polls=max_idle_polls,
        ),
        media_type="text/event-stream",
    )


@router.post("/tasks/start", response_model=StartTaskResponse)
def start_task(
    request: StartTaskRequest,
    token: gateway_write_token,
    service: GatewayControlService = Depends(gateway_service),
) -> StartTaskResponse:
    return service.start_task(request, workspace_id=token.workspace_id)


@router.post("/tasks/log", response_model=AppendTaskLogResponse)
def append_task_log(
    request: AppendTaskLogRequest,
    token: gateway_write_token,
    service: GatewayControlService = Depends(gateway_service),
) -> AppendTaskLogResponse:
    return service.append_task_log(request, workspace_id=token.workspace_id)


@router.post("/tasks/end", response_model=EndTaskResponse)
def end_task(
    request: EndTaskRequest,
    token: gateway_write_token,
    service: GatewayControlService = Depends(gateway_service),
) -> EndTaskResponse:
    return service.end_task(request, workspace_id=token.workspace_id)


async def _attach_events(
    service: GatewayControlService,
    container_id: str,
    poll_interval_seconds: float,
    *,
    workspace_id: str,
    max_events: int = 0,
    max_idle_polls: int = 0,
) -> AsyncIterator[str]:
    yield ": connected\n\n"
    emitted = 0
    emitted_events = 0
    idle_polls = 0
    while True:
        try:
            response = service.attach_to_container(
                AttachToContainerRequest(container_id=container_id),
                workspace_id=workspace_id,
            )
        except NotFoundError as exc:
            yield sse_event(
                "done",
                container_id,
                AttachToContainerResponse(done=True, exit_code=1, error_msg=exc.message),
            )
            return
        output = response.output[emitted:] if len(response.output) >= emitted else response.output
        if output:
            emitted += len(output)
            idle_polls = 0
            yield sse_event("output", container_id, response.model_copy(update={"output": output}))
            emitted_events += 1
            if max_events > 0 and emitted_events >= max_events:
                return
        if response.done:
            yield sse_event("done", container_id, response.model_copy(update={"output": ""}))
            return
        yield ": keepalive\n\n"
        idle_polls += 1
        if max_idle_polls > 0 and idle_polls >= max_idle_polls:
            return
        await asyncio.sleep(poll_interval_seconds)
