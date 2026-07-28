from __future__ import annotations

from execution.artifacts.service import ArtifactStorageService
from fastapi import APIRouter, Depends
from fastapi.responses import Response
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


@router.get("/content", operation_id="read_artifact_content")
def read_artifact_content(
    id: str,
    task_id: str,
    filename: str,
    workspace_id: read_workspace,
    download: bool = False,
    service: ArtifactStorageService = Depends(artifact_service),
) -> Response:
    """Serve an artifact's bytes from the control plane.

    Same-origin so a reader can render it without reaching the object store,
    which is usually not routable from wherever the dashboard is open.
    """
    content, content_type, name = service.read_content(
        workspace_id=workspace_id,
        task_id=task_id,
        artifact_id=id,
        filename=filename,
    )
    disposition = "attachment" if download else "inline"
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'{disposition}; filename="{name}"'},
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
