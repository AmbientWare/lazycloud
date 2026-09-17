from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import RedirectResponse
from gateway.http import (
    AuthorizeRequest,
    AuthorizeResponse,
    SignPayloadRequest,
    SignPayloadResponse,
)
from gateway.service import GatewayControlService
from shared.app_identity import WORKSPACE_OBJECT_BUCKET
from shared.http.objects import (
    AbortObjectUploadRequest,
    BeginObjectUploadResponse,
    CompleteObjectUploadRequest,
    HeadObjectRequest,
    HeadObjectResponse,
    ObjectUploadPartRequest,
    ObjectUploadPartResponse,
    PutObjectRequest,
    PutObjectResponse,
)

from api.server.auth import read_transfer, read_workspace, write_transfer, write_workspace
from api.server.dependencies import (
    AuthorizationCredentials,
    authorization_header,
)
from api.server.service_dependencies import gateway_service

router = APIRouter(prefix="/gateway", tags=["gateway"])


@router.post("/authorize", response_model=AuthorizeResponse)
def authorize(
    request: AuthorizeRequest,
    authorization: AuthorizationCredentials = None,
    service: GatewayControlService = Depends(gateway_service),
) -> AuthorizeResponse:
    return service.authorize(request, authorization_header(authorization))


@router.post("/sign-payload", response_model=SignPayloadResponse)
def sign_payload(
    request: SignPayloadRequest,
    workspace_id: write_workspace,
    service: GatewayControlService = Depends(gateway_service),
) -> SignPayloadResponse:
    return service.sign_payload(request.model_copy(update={"workspace": workspace_id}))


@router.post("/objects/head", response_model=HeadObjectResponse)
def head_object(
    request: HeadObjectRequest,
    workspace_id: read_workspace,
    service: GatewayControlService = Depends(gateway_service),
) -> HeadObjectResponse:
    return service.head_object(request, workspace_id=workspace_id)


@router.post("/objects/uploads", response_model=BeginObjectUploadResponse)
def begin_object_upload(
    request: PutObjectRequest,
    workspace_id: write_transfer,
    service: GatewayControlService = Depends(gateway_service),
) -> BeginObjectUploadResponse:
    return service.begin_object_upload(request, workspace_id=workspace_id)


@router.post("/objects/uploads/{object_id}/parts", response_model=ObjectUploadPartResponse)
def sign_object_upload_part(
    object_id: str,
    request: ObjectUploadPartRequest,
    workspace_id: write_transfer,
    service: GatewayControlService = Depends(gateway_service),
) -> ObjectUploadPartResponse:
    return service.sign_object_upload_part(object_id, request, workspace_id=workspace_id)


@router.post("/objects/uploads/{object_id}/complete", response_model=PutObjectResponse)
def complete_object_upload(
    object_id: str,
    request: CompleteObjectUploadRequest,
    workspace_id: write_transfer,
    service: GatewayControlService = Depends(gateway_service),
) -> PutObjectResponse:
    return service.complete_object_upload(object_id, request, workspace_id=workspace_id)


@router.post("/objects/uploads/{object_id}/abort", status_code=204)
def abort_object_upload(
    object_id: str,
    request: AbortObjectUploadRequest,
    workspace_id: write_workspace,
    service: GatewayControlService = Depends(gateway_service),
) -> None:
    service.abort_object_upload(object_id, request, workspace_id=workspace_id)


@router.get("/objects/download", response_class=RedirectResponse)
def download_object(
    bucket: str = Query(WORKSPACE_OBJECT_BUCKET),
    key: str = Query(...),
    expires_seconds: int = Query(3600, ge=1, le=86_400),
    *,
    workspace_id: read_transfer,
    service: GatewayControlService = Depends(gateway_service),
) -> RedirectResponse:
    return RedirectResponse(
        service.object_download_url(
            workspace_id=workspace_id,
            bucket=bucket,
            key=key,
            expires_seconds=expires_seconds,
        ),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )
