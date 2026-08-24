from __future__ import annotations

from collections.abc import Sequence

from database.records.apps import AppRecord
from fastapi import APIRouter, Depends, status
from fastapi.responses import Response
from identity.authz import token_has_scope
from operations.management import AppOperationalSummary, ManagementService
from shared.app_lifecycle import AppLifecycleState, AppLifecycleTarget
from shared.deployments import DeploymentKind
from shared.http.apps import (
    AppActionCapabilitiesResponse,
    AppCreateRequest,
    AppListResponse,
    AppResponse,
    AppSummaryListResponse,
    AppSummaryResponse,
)
from shared.http.deployments import (
    DeploymentScalingResponse,
)
from shared.http.stubs import StubResponse
from shared.identity import AuthScope

from api.server.auth import read_token, read_workspace, write_workspace
from api.server.dependencies import current_services
from api.server.response_mapping import actionable_deployment_response
from api.server.services import ApiServices

router = APIRouter()


def _app_response(record: AppRecord, *, can_write: bool) -> AppResponse:
    available = record.deleted_at is None
    retry_target = record.lifecycle_target
    return AppResponse.model_validate(record).model_copy(
        update={
            "actions": AppActionCapabilitiesResponse(
                can_pause=can_write
                and available
                and (
                    record.lifecycle_state is AppLifecycleState.Active
                    or retry_target is AppLifecycleTarget.Paused
                ),
                can_resume=can_write
                and available
                and (
                    record.lifecycle_state is AppLifecycleState.Paused
                    or retry_target is AppLifecycleTarget.Active
                ),
                can_delete=can_write
                and available
                and (
                    record.lifecycle_state in {AppLifecycleState.Active, AppLifecycleState.Paused}
                    or retry_target is AppLifecycleTarget.Deleted
                ),
            )
        }
    )


def _app_list_response(records: Sequence[AppRecord], *, can_write: bool) -> AppListResponse:
    return AppListResponse(data=[_app_response(item, can_write=can_write) for item in records])


def _app_summary_response(
    record: AppOperationalSummary,
    *,
    can_write: bool,
    scaling: dict[str, DeploymentScalingResponse],
) -> AppSummaryResponse:
    return AppSummaryResponse(
        app=_app_response(record.app, can_write=can_write),
        latest_workload=(
            StubResponse.model_validate(record.latest_workload) if record.latest_workload else None
        ),
        latest_deployment=(
            actionable_deployment_response(
                record.latest_deployment,
                can_write=can_write,
                app_active=record.app.active,
                scaling=scaling.get(record.latest_deployment.id),
            )
            if record.latest_deployment
            else None
        ),
        workload_kinds=record.workload_kinds,
        workload_count=record.workload_count,
        active_versions=record.active_versions,
        running_containers=record.running_containers,
        runs_24h=record.runs_24h,
        failed_runs_24h=record.failed_runs_24h,
        pending_runs_24h=record.pending_runs_24h,
        activity_24h=list(record.activity_24h),
        failures_24h=list(record.failures_24h),
        pending_24h=list(record.pending_24h),
        last_deployed_at=record.last_deployed_at,
    )


@router.get("/api/v1/apps", response_model=AppListResponse, operation_id="list_apps")
def list_apps(
    active: bool | None = None,
    *,
    workspace_id: read_workspace,
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> AppListResponse:
    return _app_list_response(
        services.apps.list(workspace=workspace_id, active=active),
        can_write=token_has_scope(token, AuthScope.Write),
    )


@router.post(
    "/api/v1/apps",
    response_model=AppResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_app_record",
)
def create_app_record(
    request: AppCreateRequest,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> AppResponse:
    return _app_response(
        services.apps.create(
            request.name,
            stub_id=request.stub_id,
            workspace=workspace_id,
            version=request.version,
            public=request.public,
        ),
        can_write=True,
    )


@router.get(
    "/api/v1/apps/summaries",
    response_model=AppSummaryListResponse,
    operation_id="list_app_summaries",
)
def list_app_summaries(
    workspace_id: read_workspace,
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> AppSummaryListResponse:
    can_write = token_has_scope(token, AuthScope.Write)
    management = ManagementService(services)
    summaries = management.app_summaries(workspace_id)
    scaling_states = management.pod_deployment_scaling(
        workspace_id,
        {
            item.latest_deployment.id
            for item in summaries
            if item.latest_deployment is not None
            and item.latest_deployment.kind is DeploymentKind.Pod
        },
    )
    scaling = {
        deployment_id: DeploymentScalingResponse(
            min_replicas=state.min_replicas,
            max_replicas=state.max_replicas,
        )
        for deployment_id, state in scaling_states.items()
    }
    return AppSummaryListResponse(
        items=[
            _app_summary_response(
                item,
                can_write=can_write,
                scaling=scaling,
            )
            for item in summaries
        ]
    )


@router.get(
    "/api/v1/apps/{app_id}",
    response_model=AppResponse,
    operation_id="get_app",
)
def get_app(
    app_id: str,
    workspace_id: read_workspace,
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> AppResponse:
    return _app_response(
        services.apps.get(app_id, workspace=workspace_id),
        can_write=token_has_scope(token, AuthScope.Write),
    )


@router.post(
    "/api/v1/apps/{app_id}/pause",
    response_model=AppResponse,
    operation_id="pause_app",
)
def pause_app(
    app_id: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> AppResponse:
    return _app_response(
        services.apps.pause(app_id, workspace=workspace_id),
        can_write=True,
    )


@router.post(
    "/api/v1/apps/{app_id}/resume",
    response_model=AppResponse,
    operation_id="resume_app",
)
def resume_app(
    app_id: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> AppResponse:
    return _app_response(
        services.apps.resume(app_id, workspace=workspace_id),
        can_write=True,
    )


@router.delete(
    "/api/v1/apps/{app_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_app",
)
def delete_app(
    app_id: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> None:
    services.apps.delete(app_id, workspace=workspace_id)
