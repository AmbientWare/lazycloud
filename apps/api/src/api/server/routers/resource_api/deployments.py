from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from identity.authz import token_has_scope
from shared.deployment_records import Deployment, DeploymentSpec
from shared.deployments import DeploymentKind
from shared.errors import InvalidInputError, NotFoundError
from shared.http.client_manifests import ClientManifestResource
from shared.http.deployments import (
    DeploymentActionCapabilitiesResponse,
    DeploymentListResponse,
    DeploymentPackagePlanResponse,
    DeploymentResponse,
    DeploymentScaleRequest,
    DeploymentScalingResponse,
    DeploymentStopAllResponse,
    DeploymentUrlResponse,
)
from shared.http.stubs import StubResponse
from shared.identity import AuthScope
from shared.urls import InvokeUrlMode

from api.server.auth import read_token, read_workspace, write_token, write_workspace
from api.server.dependencies import current_services
from api.server.identifiers import identifier_filter
from api.server.response_mapping import deployment_response
from api.server.routers.resource_api.common import STUB_TYPE_ALIASES, _management
from api.server.services import ApiServices

router = APIRouter()


def _deployment_response(
    deployment: Deployment,
    *,
    can_write: bool,
    app_active: bool,
    scaling: DeploymentScalingResponse | None,
) -> DeploymentResponse:
    available = deployment.deleted_at is None
    return deployment_response(
        deployment,
        scaling=scaling,
        actions=DeploymentActionCapabilitiesResponse(
            can_start=(can_write and available and app_active and not deployment.active),
            can_stop=can_write and available and deployment.active,
            can_scale=(
                can_write
                and available
                and app_active
                and deployment.active
                and deployment.kind is DeploymentKind.Pod
            ),
            can_delete=can_write and available,
        ),
    )


def _deployment_scaling_responses(
    deployments: list[Deployment] | tuple[Deployment, ...],
    *,
    workspace: str,
    services: ApiServices,
) -> dict[str, DeploymentScalingResponse]:
    states = _management(services).pod_deployment_scaling(
        workspace,
        {deployment.id for deployment in deployments if deployment.kind is DeploymentKind.Pod},
    )
    return {
        deployment_id: DeploymentScalingResponse(
            min_replicas=state.min_replicas,
            max_replicas=state.max_replicas,
        )
        for deployment_id, state in states.items()
    }


def _parsed_deployment_version(version: str) -> int | None:
    if version == "latest":
        return None
    try:
        return int(version)
    except ValueError as exc:
        msg = f"invalid deployment version: {version}"
        raise InvalidInputError(msg) from exc


def _deployment_app_active(
    deployment: Deployment,
    workspace: str,
    services: ApiServices,
) -> bool:
    if deployment.app_id is None:
        return True
    try:
        return services.apps.get(deployment.app_id, workspace=workspace).active
    except NotFoundError:
        return False


def _deployment_list_response(
    deployments: list[Deployment] | tuple[Deployment, ...],
    *,
    next_cursor: str,
    workspace: str,
    can_write: bool,
    services: ApiServices,
) -> DeploymentListResponse:
    app_states = {
        app.id: app.active for app in services.apps.list(workspace=workspace, active=None)
    }
    scaling = _deployment_scaling_responses(deployments, workspace=workspace, services=services)
    return DeploymentListResponse(
        data=[
            _deployment_response(
                deployment,
                can_write=can_write,
                app_active=(
                    True if deployment.app_id is None else app_states.get(deployment.app_id, False)
                ),
                scaling=scaling.get(deployment.id),
            )
            for deployment in deployments
        ],
        next=next_cursor,
    )


@router.get(
    "/api/v1/deployments",
    response_model=DeploymentListResponse,
    operation_id="list_deployments",
)
def list_deployments(
    active: bool | None = None,
    app_id: identifier_filter = None,
    name: str | None = None,
    latest: bool = False,
    limit: int = 100,
    cursor: str | None = None,
    *,
    workspace_id: read_workspace,
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> DeploymentListResponse:
    management = _management(services)
    can_write = token_has_scope(token, AuthScope.Write)
    if latest:
        page = management.latest_deployments(
            workspace_id,
            app_id=app_id,
            name=name,
            limit=limit,
        )
        return _deployment_list_response(
            list(page.data),
            next_cursor=page.next,
            workspace=workspace_id,
            can_write=can_write,
            services=services,
        )
    page = management.list_deployments(
        workspace_id,
        active=active,
        app_id=app_id,
        name=name,
        limit=limit,
        cursor=cursor,
    )
    return _deployment_list_response(
        list(page.data),
        next_cursor=page.next,
        workspace=workspace_id,
        can_write=can_write,
        services=services,
    )


@router.post(
    "/api/v1/deployments",
    response_model=DeploymentResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_deployment",
)
def create_deployment(
    request: DeploymentSpec,
    token: write_token,
    services: ApiServices = Depends(current_services),
) -> DeploymentResponse:
    deployment = services.deployments.deploy(request, workspace=token.workspace_id)
    scaling = _deployment_scaling_responses(
        [deployment], workspace=token.workspace_id, services=services
    )
    return _deployment_response(
        deployment,
        can_write=True,
        app_active=_deployment_app_active(deployment, token.workspace_id, services),
        scaling=scaling.get(deployment.id),
    )


@router.post(
    "/api/v1/deployments/stop-all",
    response_model=DeploymentStopAllResponse,
    operation_id="stop_all_active_deployments",
)
def stop_all_active_deployments(
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> DeploymentStopAllResponse:
    stopped = _management(services).stop_all_active_deployments(workspace_id)
    return DeploymentStopAllResponse(
        stopped=_deployment_list_response(
            stopped,
            next_cursor="",
            workspace=workspace_id,
            can_write=True,
            services=services,
        ).data
    )


@router.get(
    "/api/v1/deployments/by-name/{stub_type}/{deployment_name}/{version}/url",
    response_model=DeploymentUrlResponse,
    operation_id="get_deployment_url_by_name",
)
def deployment_url_by_name(
    stub_type: str,
    deployment_name: str,
    version: str,
    external_url: str = "http://127.0.0.1:9000",
    mode: InvokeUrlMode = InvokeUrlMode.Path,
    *,
    workspace_id: read_workspace,
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> DeploymentUrlResponse:
    if stub_type not in STUB_TYPE_ALIASES:
        msg = f"invalid stub type: {stub_type}"
        raise InvalidInputError(msg)
    parsed_version = _parsed_deployment_version(version)
    result = _management(services).deployment_url_by_name(
        workspace_id,
        STUB_TYPE_ALIASES[stub_type],
        deployment_name,
        parsed_version,
        external_url=external_url,
        mode=mode,
    )
    return DeploymentUrlResponse(
        deployment=_deployment_response(
            result.deployment,
            can_write=token_has_scope(token, AuthScope.Write),
            app_active=_deployment_app_active(result.deployment, workspace_id, services),
            scaling=_deployment_scaling_responses(
                [result.deployment], workspace=workspace_id, services=services
            ).get(result.deployment.id),
        ),
        stub=StubResponse.model_validate(result.stub) if result.stub is not None else None,
        url=result.url,
    )


@router.get(
    "/api/v1/deployments/{stub_id}/download",
    response_model=None,
    operation_id="download_deployment_package",
)
def download_deployment_package(
    stub_id: str,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> Response | DeploymentPackagePlanResponse:
    plan = _management(services).deployment_package(workspace_id, stub_id)
    if plan.path:
        return FileResponse(
            plan.path,
            media_type=plan.object.content_type if plan.object else "application/octet-stream",
            filename=plan.filename,
        )
    if plan.presigned_url:
        return RedirectResponse(plan.presigned_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    return DeploymentPackagePlanResponse.model_validate(plan)


@router.get(
    "/api/v1/deployments/{deployment_id}/url",
    response_model=DeploymentUrlResponse,
    operation_id="get_deployment_url",
)
def deployment_url(
    deployment_id: str,
    external_url: str = "http://127.0.0.1:9000",
    mode: InvokeUrlMode = InvokeUrlMode.Path,
    *,
    workspace_id: read_workspace,
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> DeploymentUrlResponse:
    result = _management(services).deployment_url(
        deployment_id,
        workspace=workspace_id,
        external_url=external_url,
        mode=mode,
    )
    return DeploymentUrlResponse(
        deployment=_deployment_response(
            result.deployment,
            can_write=token_has_scope(token, AuthScope.Write),
            app_active=_deployment_app_active(result.deployment, workspace_id, services),
            scaling=_deployment_scaling_responses(
                [result.deployment], workspace=workspace_id, services=services
            ).get(result.deployment.id),
        ),
        stub=StubResponse.model_validate(result.stub) if result.stub is not None else None,
        url=result.url,
    )


@router.get(
    "/api/v1/deployments/{deployment_id}/manifest",
    response_model=ClientManifestResource,
    operation_id="get_deployment_manifest",
)
def deployment_manifest(
    deployment_id: str,
    external_url: str = "http://127.0.0.1:9000",
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> ClientManifestResource:
    return _management(services).deployment_manifest(
        deployment_id,
        workspace=workspace_id,
        external_url=external_url,
    )


@router.post(
    "/api/v1/deployments/{deployment_id}/stop",
    response_model=DeploymentResponse,
    operation_id="stop_deployment",
)
def stop_deployment(
    deployment_id: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> DeploymentResponse:
    deployment = _management(services).set_deployment_active(
        workspace_id,
        deployment_id,
        active=False,
    )
    return _deployment_response(
        deployment,
        can_write=True,
        app_active=_deployment_app_active(deployment, workspace_id, services),
        scaling=_deployment_scaling_responses(
            [deployment], workspace=workspace_id, services=services
        ).get(deployment.id),
    )


@router.post(
    "/api/v1/deployments/{deployment_id}/start",
    response_model=DeploymentResponse,
    operation_id="start_deployment",
)
def start_deployment(
    deployment_id: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> DeploymentResponse:
    deployment = _management(services).set_deployment_active(
        workspace_id,
        deployment_id,
        active=True,
    )
    return _deployment_response(
        deployment,
        can_write=True,
        app_active=_deployment_app_active(deployment, workspace_id, services),
        scaling=_deployment_scaling_responses(
            [deployment], workspace=workspace_id, services=services
        ).get(deployment.id),
    )


@router.post(
    "/api/v1/deployments/{deployment_id}/scale",
    response_model=DeploymentResponse,
    operation_id="scale_deployment",
)
def scale_deployment(
    deployment_id: str,
    request: DeploymentScaleRequest,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> DeploymentResponse:
    deployment = _management(services).scale_deployment(
        workspace_id,
        deployment_id,
        containers=request.replicas,
    )
    return _deployment_response(
        deployment,
        can_write=True,
        app_active=_deployment_app_active(deployment, workspace_id, services),
        scaling=_deployment_scaling_responses(
            [deployment], workspace=workspace_id, services=services
        ).get(deployment.id),
    )


@router.get(
    "/api/v1/deployments/{deployment_id}",
    response_model=DeploymentResponse,
    operation_id="get_deployment",
)
def get_deployment(
    deployment_id: str,
    workspace_id: read_workspace,
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> DeploymentResponse:
    deployment = _management(services).retrieve_deployment(workspace_id, deployment_id)
    return _deployment_response(
        deployment,
        can_write=token_has_scope(token, AuthScope.Write),
        app_active=_deployment_app_active(deployment, workspace_id, services),
        scaling=_deployment_scaling_responses(
            [deployment], workspace=workspace_id, services=services
        ).get(deployment.id),
    )


@router.delete(
    "/api/v1/deployments/{deployment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_deployment",
)
def delete_deployment(
    deployment_id: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> None:
    _management(services).delete_deployment(workspace_id, deployment_id)
