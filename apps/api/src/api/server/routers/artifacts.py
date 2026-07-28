from __future__ import annotations

from execution.artifacts.service import ArtifactStorageService
from fastapi import APIRouter, Depends
from shared.http.artifacts import (
    ArtifactListResponse,
    ArtifactPublicUrlRequest,
    ArtifactPublicUrlResponse,
    ArtifactSaveBody,
    ArtifactSaveResponse,
    ArtifactStat,
    ArtifactStatRequest,
    ArtifactStatResponse,
    ArtifactSummary,
)

from api.server.auth import read_workspace, write_workspace
from api.server.service_dependencies import artifact_service

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifact"])


@router.post("/save", response_model=ArtifactSaveResponse)
def save_artifact(
    request: ArtifactSaveBody,
    workspace_id: write_workspace,
    service: ArtifactStorageService = Depends(artifact_service),
) -> ArtifactSaveResponse:
    artifact_id = service.save(
        workspace_id=workspace_id,
        task_id=request.task_id,
        filename=request.filename,
        content=request.bytes_value(),
        content_type=request.content_type,
    )
    return ArtifactSaveResponse(id=artifact_id)


@router.get("", response_model=ArtifactListResponse, operation_id="list_artifacts")
def list_artifacts(
    task_id: str,
    workspace_id: read_workspace,
    service: ArtifactStorageService = Depends(artifact_service),
) -> ArtifactListResponse:
    listings = service.list_for_task(workspace_id=workspace_id, task_id=task_id)
    return ArtifactListResponse(
        data=[
            ArtifactSummary(
                id=item.artifact_id,
                task_id=item.task_id,
                filename=item.filename,
                content_type=item.content_type,
                size=item.size,
                created_at=item.created_at,
            )
            for item in listings
        ]
    )


@router.post("/stat", response_model=ArtifactStatResponse)
def stat_artifact(
    request: ArtifactStatRequest,
    workspace_id: read_workspace,
    service: ArtifactStorageService = Depends(artifact_service),
) -> ArtifactStatResponse:
    plan = service.stat(
        workspace_id=workspace_id,
        task_id=request.task_id,
        artifact_id=request.id,
        filename=request.filename,
    )
    return ArtifactStatResponse(
        stat=ArtifactStat(
            mode=plan.mode,
            size=plan.size,
            atime=plan.accessed_at,
            mtime=plan.modified_at,
        )
    )


@router.post("/public-url", response_model=ArtifactPublicUrlResponse)
def artifact_public_url(
    request: ArtifactPublicUrlRequest,
    workspace_id: read_workspace,
    service: ArtifactStorageService = Depends(artifact_service),
) -> ArtifactPublicUrlResponse:
    plan = service.public_url(
        workspace_id=workspace_id,
        task_id=request.task_id,
        artifact_id=request.id,
        filename=request.filename,
        gateway_external_url=request.gateway_external_url,
        expires_seconds=request.expires,
    )
    return ArtifactPublicUrlResponse(public_url=plan.public_url)
