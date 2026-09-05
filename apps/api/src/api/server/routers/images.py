from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from images.control import ImageControlService
from shared.http.images import (
    BuildImageEvent,
    BuildImageRequest,
    BuildImageResponse,
    VerifyImageBuildRequest,
    VerifyImageBuildResponse,
)

from api.server.auth import read_workspace, write_workspace
from api.server.service_dependencies import image_service

router = APIRouter(tags=["images"])


@router.post(
    "/api/v1/images/verify-build",
    response_model=VerifyImageBuildResponse,
    operation_id="verify_image_build",
)
def verify_image_build(
    request: VerifyImageBuildRequest,
    workspace_id: write_workspace,
    service: ImageControlService = Depends(image_service),
) -> VerifyImageBuildResponse:
    return service.verify_image_build(request, workspace_id=workspace_id)


@router.post("/api/v1/images/build", response_class=StreamingResponse, operation_id="build_image")
def build_image(
    request: BuildImageRequest,
    workspace_id: write_workspace,
    service: ImageControlService = Depends(image_service),
) -> StreamingResponse:
    return StreamingResponse(
        _ndjson(service.build_image(request, workspace_id=workspace_id)),
        media_type="application/x-ndjson",
    )


@router.get(
    "/api/v1/image-builds/{build_id}/events",
    response_class=StreamingResponse,
    operation_id="stream_image_build_events",
)
def image_build_events(
    build_id: str,
    workspace_id: read_workspace,
    after: int = Query(default=0, ge=0),
    service: ImageControlService = Depends(image_service),
) -> StreamingResponse:
    return StreamingResponse(
        _ndjson(service.follow_build(build_id, workspace_id=workspace_id, after=after)),
        media_type="application/x-ndjson",
    )


def _ndjson(items: Iterator[BuildImageResponse | BuildImageEvent]) -> Iterator[bytes]:
    for item in items:
        yield (json.dumps(item.model_dump(mode="json"), separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
