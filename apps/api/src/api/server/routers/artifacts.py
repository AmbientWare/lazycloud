from __future__ import annotations

from datetime import datetime
from uuid import UUID

from execution.artifacts.service import ArtifactStorageService
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from shared.artifacts import InheritRetention
from shared.http.artifacts import (
    ArtifactListResponse,
    ArtifactPublicUrlRequest,
    ArtifactPublicUrlResponse,
    ArtifactRetentionPolicy,
    ArtifactRetentionPreview,
    ArtifactRetentionSelection,
    ArtifactRetentionUpdate,
    ArtifactSaveBody,
    ArtifactSaveResponse,
    ArtifactStat,
    ArtifactStatRequest,
    ArtifactStatResponse,
    ArtifactStorageSummary,
    ArtifactSummary,
)
from shared.http_headers import INLINE_RENDERABLE_CONTENT_TYPES, content_disposition

from api.server.auth import read_transfer, read_workspace, write_transfer, write_workspace
from api.server.public_transfers import attribute_public_transfer
from api.server.service_dependencies import artifact_service

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifact"])


@router.post("/save", response_model=ArtifactSaveResponse)
def save_artifact(
    request: ArtifactSaveBody,
    workspace_id: write_transfer,
    service: ArtifactStorageService = Depends(artifact_service),
) -> ArtifactSaveResponse:
    return service.save(
        workspace_id=workspace_id,
        task_id=request.task_id,
        filename=request.filename,
        content=request.bytes_value(),
        content_type=request.content_type,
        retention_seconds=request.retention_seconds
        if "retention_seconds" in request.model_fields_set
        else InheritRetention.Workspace,
    )


@router.get("", response_model=ArtifactListResponse, operation_id="list_artifacts")
def list_artifacts(
    workspace_id: read_workspace,
    task_id: str | None = None,
    app_id: str | None = None,
    search: str = "",
    content_type: str = "",
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    cursor: str = "",
    limit: int = Query(default=50, ge=1, le=100),
    service: ArtifactStorageService = Depends(artifact_service),
) -> ArtifactListResponse:
    return service.list(
        workspace_id=workspace_id,
        task_id=task_id,
        app_id=app_id,
        search=search,
        content_type=content_type,
        created_after=created_after,
        created_before=created_before,
        cursor=cursor,
        limit=limit,
    )


@router.get("/summary", operation_id="artifact_storage_summary")
def artifact_storage_summary(
    workspace_id: read_workspace, service: ArtifactStorageService = Depends(artifact_service)
) -> ArtifactStorageSummary:
    return service.summary(workspace_id=workspace_id)


@router.put("/retention", operation_id="set_artifact_retention")
def set_artifact_retention(
    request: ArtifactRetentionUpdate,
    workspace_id: write_workspace,
    service: ArtifactStorageService = Depends(artifact_service),
) -> ArtifactRetentionPolicy:
    return service.set_workspace_retention(
        workspace_id=workspace_id, retention_seconds=request.retention_seconds
    )


@router.post("/retention/preview", operation_id="preview_artifact_retention")
def preview_artifact_retention(
    request: ArtifactRetentionSelection,
    workspace_id: read_workspace,
    service: ArtifactStorageService = Depends(artifact_service),
) -> ArtifactRetentionPreview:
    return service.retention_selection(workspace_id=workspace_id, request=request)


@router.post("/retention/apply", operation_id="apply_artifact_retention")
def apply_artifact_retention(
    request: ArtifactRetentionSelection,
    workspace_id: write_workspace,
    service: ArtifactStorageService = Depends(artifact_service),
) -> ArtifactRetentionPreview:
    return service.retention_selection(workspace_id=workspace_id, request=request, apply=True)


@router.patch("/{artifact_id}/retention", operation_id="update_artifact_retention")
def update_artifact_retention(
    artifact_id: UUID,
    request: ArtifactRetentionUpdate,
    workspace_id: write_workspace,
    service: ArtifactStorageService = Depends(artifact_service),
) -> ArtifactSummary:
    return service.update_retention(
        workspace_id=workspace_id,
        artifact_id=str(artifact_id),
        retention_seconds=request.retention_seconds,
    )


@router.delete("/{artifact_id}", status_code=204, operation_id="delete_artifact")
def delete_artifact(
    artifact_id: UUID,
    workspace_id: write_workspace,
    service: ArtifactStorageService = Depends(artifact_service),
) -> None:
    service.delete(workspace_id=workspace_id, artifact_id=str(artifact_id))


@router.get("/content", operation_id="read_artifact_content")
def read_artifact_content(
    request: Request,
    id: str,
    task_id: str,
    filename: str,
    workspace_id: read_transfer,
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
    inline = not download and content_type in INLINE_RENDERABLE_CONTENT_TYPES
    attribute_public_transfer(
        request,
        workspace_id=workspace_id,
        resource_type="artifact",
        resource_id=id,
    )
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": content_disposition(name, inline=inline),
            # The content type is whatever the task declared when it saved the
            # file, so the browser must not be free to reinterpret it.
            "X-Content-Type-Options": "nosniff",
        },
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
    workspace_id: read_transfer,
    service: ArtifactStorageService = Depends(artifact_service),
) -> ArtifactPublicUrlResponse:
    plan = service.public_url(
        workspace_id=workspace_id,
        task_id=request.task_id,
        artifact_id=request.id,
        filename=request.filename,
        expires_seconds=request.expires,
    )
    return ArtifactPublicUrlResponse(public_url=plan.public_url)
