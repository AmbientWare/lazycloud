from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from images.control import ImageControlService
from shared.http.images import (
    BuildImageRequest,
    BuildImageResponse,
    VerifyImageBuildRequest,
    VerifyImageBuildResponse,
)

from api.server.auth import write_workspace
from api.server.service_dependencies import image_service

router = APIRouter(prefix="/api/v1/images", tags=["images"])


@router.post(
    "/verify-build",
    response_model=VerifyImageBuildResponse,
    operation_id="verify_image_build",
)
def verify_image_build(
    request: VerifyImageBuildRequest,
    workspace_id: write_workspace,
    service: ImageControlService = Depends(image_service),
) -> VerifyImageBuildResponse:
    return service.verify_image_build(request, workspace_id=workspace_id)


@router.post("/build", response_class=StreamingResponse, operation_id="build_image")
def build_image(
    request: BuildImageRequest,
    workspace_id: write_workspace,
    service: ImageControlService = Depends(image_service),
) -> StreamingResponse:
    return StreamingResponse(
        _ndjson(service.build_image(request, workspace_id=workspace_id)),
        media_type="application/x-ndjson",
    )


def _ndjson(items: Iterator[BuildImageResponse]) -> Iterator[bytes]:
    for item in items:
        yield (json.dumps(item.model_dump(mode="json"), separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
