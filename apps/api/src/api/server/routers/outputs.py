from __future__ import annotations

from execution.outputs.service import OutputStorageService
from fastapi import APIRouter, Depends
from shared.http.outputs import (
    OutputPublicUrlRequest,
    OutputPublicUrlResponse,
    OutputSaveBody,
    OutputSaveResponse,
    OutputStat,
    OutputStatRequest,
    OutputStatResponse,
)

from api.server.auth import read_workspace, write_workspace
from api.server.service_dependencies import output_service

router = APIRouter(prefix="/api/v1/outputs", tags=["output"])


@router.post("/save", response_model=OutputSaveResponse)
def save_output(
    request: OutputSaveBody,
    workspace_id: write_workspace,
    service: OutputStorageService = Depends(output_service),
) -> OutputSaveResponse:
    output_id = service.save(
        workspace_id=workspace_id,
        task_id=request.task_id,
        filename=request.filename,
        content=request.bytes_value(),
        content_type=request.content_type,
    )
    return OutputSaveResponse(id=output_id)


@router.post("/stat", response_model=OutputStatResponse)
def stat_output(
    request: OutputStatRequest,
    workspace_id: read_workspace,
    service: OutputStorageService = Depends(output_service),
) -> OutputStatResponse:
    plan = service.stat(
        workspace_id=workspace_id,
        task_id=request.task_id,
        output_id=request.id,
        filename=request.filename,
    )
    return OutputStatResponse(
        stat=OutputStat(
            mode=plan.mode,
            size=plan.size,
            atime=plan.accessed_at,
            mtime=plan.modified_at,
        )
    )


@router.post("/public-url", response_model=OutputPublicUrlResponse)
def output_public_url(
    request: OutputPublicUrlRequest,
    workspace_id: read_workspace,
    service: OutputStorageService = Depends(output_service),
) -> OutputPublicUrlResponse:
    plan = service.public_url(
        workspace_id=workspace_id,
        task_id=request.task_id,
        output_id=request.id,
        filename=request.filename,
        gateway_external_url=request.gateway_external_url,
        expires_seconds=request.expires,
    )
    return OutputPublicUrlResponse(public_url=plan.public_url)
