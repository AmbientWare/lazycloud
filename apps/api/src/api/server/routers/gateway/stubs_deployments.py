from __future__ import annotations

from fastapi import APIRouter, Depends
from gateway.service import GatewayControlService
from shared.http.client_manifests import ClientManifestRequest, ClientManifestResponse
from shared.http.gateway import (
    DeployStubRequest,
    DeployStubResponse,
    GetOrCreateStubRequest,
    GetOrCreateStubResponse,
    GetUrlRequest,
    GetUrlResponse,
    ResolveDeploymentTargetRequest,
    ResolveDeploymentTargetResponse,
)

from api.server.auth import read_token, write_token
from api.server.service_dependencies import gateway_service

router = APIRouter(prefix="/gateway", tags=["gateway"])


@router.post("/stubs/get-or-create", response_model=GetOrCreateStubResponse)
def get_or_create_stub(
    request: GetOrCreateStubRequest,
    token: write_token,
    service: GatewayControlService = Depends(gateway_service),
) -> GetOrCreateStubResponse:
    return service.get_or_create_stub(request.model_copy(update={"workspace": token.workspace_id}))


@router.post("/stubs/deploy", response_model=DeployStubResponse)
def deploy_stub(
    request: DeployStubRequest,
    token: write_token,
    service: GatewayControlService = Depends(gateway_service),
) -> DeployStubResponse:
    return service.deploy_stub(request.model_copy(update={"workspace": token.workspace_id}))


@router.post("/stubs/url", response_model=GetUrlResponse)
def get_url(
    request: GetUrlRequest,
    token: read_token,
    service: GatewayControlService = Depends(gateway_service),
) -> GetUrlResponse:
    return service.get_url(request.model_copy(update={"workspace": token.workspace_id}))


@router.post("/deployments/resolve-target", response_model=ResolveDeploymentTargetResponse)
def resolve_deployment_target(
    request: ResolveDeploymentTargetRequest,
    token: read_token,
    service: GatewayControlService = Depends(gateway_service),
) -> ResolveDeploymentTargetResponse:
    return service.resolve_deployment_target(
        request.model_copy(update={"workspace": token.workspace_id})
    )


@router.post("/client-manifests", response_model=ClientManifestResponse)
def client_manifest(
    request: ClientManifestRequest,
    token: read_token,
    service: GatewayControlService = Depends(gateway_service),
) -> ClientManifestResponse:
    return service.client_manifest(request.model_copy(update={"workspace": token.workspace_id}))
