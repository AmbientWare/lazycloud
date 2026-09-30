from __future__ import annotations

from collections.abc import Sequence

from control.models import StubCloneResult, StubConfigUpdateResult, StubUrlPlan
from control.service import ControlServices
from database.records.apps import StubRecord
from fastapi import APIRouter, Depends, HTTPException, status
from shared.http.apps import StubCloneResponse
from shared.http.pods import SandboxListResponse, SandboxStatsResponse, SandboxTimeline
from shared.http.stubs import (
    PublicStubConfigResponse,
    StubCloneRequest,
    StubConfigUpdateRequest,
    StubConfigUpdateResponse,
    StubCreateRequest,
    StubListResponse,
    StubResponse,
    StubUrlResponse,
)
from shared.identity import AuthScope

from api.server.auth import read_workspace, write_principal, write_workspace
from api.server.dependencies import (
    authorize_token_workspace,
    management_services,
)
from api.server.service_dependencies import control_plane_service
from api.server.services import ManagementServiceCore

router = APIRouter()


def _stub_list_response(records: Sequence[StubRecord]) -> StubListResponse:
    return StubListResponse(stubs=[StubResponse.model_validate(item) for item in records])


def _stub_url_response(result: StubUrlPlan) -> StubUrlResponse:
    deployment = result.deployment
    return StubUrlResponse(
        stub=StubResponse.model_validate(result.stub),
        url=result.url,
        external_url=result.external_url,
        route_kind=result.route_kind,
        deployment_id=deployment.id if deployment is not None else None,
        deployment_name=deployment.name if deployment is not None else None,
        deployment_version=deployment.version if deployment is not None else None,
    )


@router.get("/api/v1/stubs", response_model=StubListResponse, operation_id="list_stubs")
def list_stubs(
    app_id: str | None = None,
    deployed_only: bool = False,
    *,
    workspace_id: read_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> StubListResponse:
    return _stub_list_response(
        service.stubs.list_stubs(
            workspace=workspace_id,
            app_id=app_id,
            deployed_only=deployed_only,
        )
    )


@router.post(
    "/api/v1/stubs",
    response_model=StubResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_stub",
)
def create_stub(
    request: StubCreateRequest,
    workspace_id: write_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> StubResponse:
    return StubResponse.model_validate(
        service.stubs.create_stub(
            request.name,
            workspace=workspace_id,
            kind=request.kind,
            handler=request.handler,
            deployment_id=request.deployment_id,
            app_id=request.app_id,
            public=request.public,
            config=request.config.model_dump(mode="json", exclude_none=True),
            metadata=request.metadata,
        )
    )


@router.get(
    "/api/v1/stubs/sandboxes/stats",
    response_model=SandboxStatsResponse,
    operation_id="get_sandbox_stats",
)
def sandbox_stats(
    app_id: str | None = None,
    *,
    workspace_id: read_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> SandboxStatsResponse:
    return service.sandboxes.sandbox_stats(workspace=workspace_id, app_id=app_id)


@router.get(
    "/api/v1/stubs/sandboxes/{stub_id}/timeline",
    response_model=SandboxTimeline,
    operation_id="get_sandbox_timeline",
)
def sandbox_timeline(
    stub_id: str,
    container_id: str | None = None,
    *,
    workspace_id: read_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> SandboxTimeline:
    return service.sandboxes.sandbox_timeline(
        stub_id,
        workspace=workspace_id,
        container_id=container_id,
    )


@router.get(
    "/api/v1/stubs/sandboxes",
    response_model=SandboxListResponse,
    operation_id="list_sandboxes",
)
def list_sandboxes(
    app_id: str | None = None,
    limit: int = 50,
    *,
    workspace_id: read_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> SandboxListResponse:
    return service.sandboxes.list_sandbox_rows(
        workspace=workspace_id,
        app_id=app_id,
        limit=limit,
    )


@router.get(
    "/api/v1/stubs/{stub_id}/config",
    response_model=PublicStubConfigResponse,
    operation_id="get_public_stub_config",
)
def get_public_stub_config(
    stub_id: str,
    service: ControlServices = Depends(control_plane_service),
) -> PublicStubConfigResponse:
    try:
        return PublicStubConfigResponse.model_validate(service.stubs.get_stub_config(stub_id))
    except PermissionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "stub config not found") from exc


@router.post(
    "/api/v1/stubs/{stub_id}/clone",
    response_model=StubCloneResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="clone_stub",
)
def clone_stub(
    stub_id: str,
    request: StubCloneRequest,
    principal: write_principal,
    services: ManagementServiceCore = Depends(management_services),
    service: ControlServices = Depends(control_plane_service),
) -> StubCloneResult:
    workspace_id = authorize_token_workspace(
        services,
        principal.token,
        request.workspace,
        AuthScope.Write,
        platform_role=principal.platform_role,
    )
    return service.cloning.clone_stub(
        stub_id, apps=services.apps, workspace=workspace_id, overrides=request.overrides
    )


@router.get(
    "/api/v1/stubs/{stub_id}/url",
    response_model=StubUrlResponse,
    operation_id="get_stub_url",
)
def stub_url(
    stub_id: str,
    deployment_id: str | None = None,
    external_url: str = "http://127.0.0.1:9000",
    port: int | None = None,
    *,
    workspace_id: read_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> StubUrlResponse:
    return _stub_url_response(
        service.stubs.stub_url(
            stub_id,
            workspace=workspace_id,
            external_url=external_url,
            deployment_id=deployment_id,
            port=port,
        )
    )


@router.patch(
    "/api/v1/stubs/{stub_id}/config",
    response_model=StubConfigUpdateResponse,
    operation_id="update_stub_config",
)
def update_stub_config(
    stub_id: str,
    request: StubConfigUpdateRequest,
    workspace_id: write_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> StubConfigUpdateResult:
    return service.stubs.update_stub_config(
        stub_id,
        workspace=workspace_id,
        fields=request.fields(),
    )


@router.get(
    "/api/v1/stubs/{stub_id}",
    response_model=StubResponse,
    operation_id="get_stub",
)
def get_stub(
    stub_id: str,
    workspace_id: read_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> StubResponse:
    return StubResponse.model_validate(service.stubs.get_stub(stub_id, workspace=workspace_id))
