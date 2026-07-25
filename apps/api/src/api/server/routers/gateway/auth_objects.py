from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import RedirectResponse
from gateway.http import (
    AuthorizeRequest,
    AuthorizeResponse,
    SignPayloadRequest,
    SignPayloadResponse,
)
from gateway.service import GatewayControlService
from pydantic import ValidationError
from shared.app_identity import WORKSPACE_OBJECT_BUCKET
from shared.errors import InvalidInputError
from shared.http.objects import (
    HeadObjectRequest,
    HeadObjectResponse,
    ObjectMetadata,
    PutObjectRequest,
    PutObjectResponse,
)

from api.server.auth import read_token, write_token
from api.server.dependencies import AuthorizationCredentials, authorization_header
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
    token: write_token,
    service: GatewayControlService = Depends(gateway_service),
) -> SignPayloadResponse:
    return service.sign_payload(request.model_copy(update={"workspace": token.workspace_id}))


@router.post("/objects/head", response_model=HeadObjectResponse)
def head_object(
    request: HeadObjectRequest,
    token: read_token,
    service: GatewayControlService = Depends(gateway_service),
) -> HeadObjectResponse:
    return service.head_object(request, workspace_id=token.workspace_id)


@router.post("/objects/stream", response_model=PutObjectResponse)
async def put_object_stream(
    request: Request,
    token: write_token,
    name: str = Query("", max_length=1024),
    object_hash: str = Query(
        ...,
        alias="hash",
        min_length=64,
        max_length=64,
        pattern="^[0-9a-f]{64}$",
    ),
    size: int = Query(..., ge=0),
    bucket: str = Query(WORKSPACE_OBJECT_BUCKET, min_length=1, max_length=63),
    overwrite: bool = Query(False),
    content_length: int | None = Header(None, ge=0),
    service: GatewayControlService = Depends(gateway_service),
) -> PutObjectResponse:
    if content_length is not None and content_length != size:
        raise InvalidInputError("content length does not match object size")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    try:
        upload_request = PutObjectRequest(
            object_metadata=ObjectMetadata(
                name=name,
                size=size,
            ),
            hash=object_hash,
            bucket=bucket,
            overwrite=overwrite,
            content_type=media_type or "application/octet-stream",
            metadata=_metadata_from_headers(request),
        )
    except ValidationError as exc:
        raise InvalidInputError("invalid object upload metadata") from exc
    return await service.put_object_chunks(
        upload_request,
        request.stream(),
        workspace_id=token.workspace_id,
    )


@router.get("/objects/download", response_class=RedirectResponse)
def download_object(
    bucket: str = Query(WORKSPACE_OBJECT_BUCKET),
    key: str = Query(...),
    expires_seconds: int = Query(3600, ge=1, le=86_400),
    *,
    token: read_token,
    service: GatewayControlService = Depends(gateway_service),
) -> RedirectResponse:
    return RedirectResponse(
        service.object_download_url(
            workspace_id=token.workspace_id,
            bucket=bucket,
            key=key,
            expires_seconds=expires_seconds,
        ),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


def _metadata_from_headers(request: Request) -> dict[str, str]:
    metadata: dict[str, str] = {}
    prefix = "x-object-meta-"
    for key, value in request.headers.items():
        lowered = key.lower()
        if lowered.startswith(prefix):
            metadata[lowered.removeprefix(prefix)] = value
    return metadata
