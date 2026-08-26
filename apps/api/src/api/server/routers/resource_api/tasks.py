from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from execution.functions.service import FunctionControlService
from execution.task_rerun import TaskRerunService
from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from identity.authz import token_has_scope
from operations.management import TaskDetailView, TaskView
from shared.deployments import StubKind
from shared.http.compute import ContainerResponse
from shared.http.functions import FunctionCallGraphResponse
from shared.http.tasks import (
    TaskCountByDeploymentListResponse,
    TaskCountByDeploymentResponse,
    TaskDetailResponse,
    TaskLogEntryResponse,
    TaskLogListResponse,
    TaskMetricsSummaryResponse,
    TaskPageResponse,
    TaskResponse,
    TaskStopResponse,
    TaskTimeWindowBucketListResponse,
    TaskTimeWindowBucketResponse,
)
from shared.identity import AuthScope
from shared.tasks import Task, TaskStatus

from api.server.auth import read_token, read_workspace, write_workspace
from api.server.dependencies import current_services
from api.server.identifiers import identifier_filter
from api.server.routers.resource_api.common import _management
from api.server.service_dependencies import task_rerun_service
from api.server.services import ApiServices

router = APIRouter()


def _task_payload[TPayload: TaskResponse](model: type[TPayload], task: Task) -> TPayload:
    """A task rendered into `model`, with the durable function result published.

    The stored result is a typed envelope the public contract states as JSON, so
    it is projected here rather than left for a serializer to guess at.
    """

    response = model.model_validate(task)
    if task.function_result is None:
        return response
    return response.model_copy(update={"result": task.function_result.model_dump(mode="json")})


def _task_response(task: Task) -> TaskResponse:
    return _task_payload(TaskResponse, task)


def _task_view_response(view: TaskView) -> TaskResponse:
    return _task_payload(TaskResponse, view.task).model_copy(
        update={
            "app": view.app,
            "workload": view.workload,
            "deployment": view.deployment,
            "actions": view.actions,
        }
    )


def _task_detail_response(view: TaskDetailView) -> TaskDetailResponse:
    return _task_payload(TaskDetailResponse, view.task).model_copy(
        update={
            "app": view.app,
            "workload": view.workload,
            "deployment": view.deployment,
            "actions": view.actions,
            "container": (
                ContainerResponse.model_validate(view.container)
                if view.container is not None
                else None
            ),
        }
    )


@router.get("/api/v1/tasks", response_model=TaskPageResponse, operation_id="list_tasks")
def list_tasks(
    stub_ids: Annotated[list[str], Query(default_factory=list, alias="stub_id")],
    status_filter: TaskStatus | None = Query(default=None, alias="status"),
    deployment_id: identifier_filter = None,
    app_id: identifier_filter = None,
    kind: StubKind | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    created_within_seconds: int | None = Query(default=None, ge=1, le=31_536_000),
    search: str | None = Query(default=None, alias="q", max_length=240),
    root_only: bool = False,
    limit: int = Query(default=50, ge=1, le=10_000),
    cursor: str | None = None,
    *,
    workspace_id: read_workspace,
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> TaskPageResponse:
    page = _management(services).task_page(
        workspace_id,
        status=status_filter,
        deployment_id=deployment_id,
        app_id=app_id,
        stub_ids=tuple(stub_ids),
        kind=kind,
        created_after=(
            created_after
            if created_after is not None
            else datetime.now(UTC) - timedelta(seconds=created_within_seconds)
            if created_within_seconds is not None
            else None
        ),
        created_before=created_before,
        search=search,
        root_only=root_only,
        can_write=token_has_scope(token, AuthScope.Write),
        limit=limit,
        cursor=cursor,
    )
    return TaskPageResponse(
        data=[_task_view_response(task) for task in page.data],
        next=page.next,
    )


@router.get(
    "/api/v1/tasks/metrics",
    response_model=TaskMetricsSummaryResponse,
    operation_id="get_task_metrics",
)
def task_metrics(
    started_at: int,
    ended_at: int,
    app_id: identifier_filter = None,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> TaskMetricsSummaryResponse:
    return TaskMetricsSummaryResponse.model_validate(
        _management(services).task_metrics(
            workspace=workspace_id,
            started_at=datetime.fromtimestamp(started_at, UTC),
            ended_at=datetime.fromtimestamp(ended_at, UTC),
            app_id=app_id,
        )
    )


@router.get(
    "/api/v1/tasks/count-by-deployment",
    response_model=TaskCountByDeploymentListResponse,
    operation_id="get_task_count_by_deployment",
)
def task_count_by_deployment(
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> TaskCountByDeploymentListResponse:
    return TaskCountByDeploymentListResponse(
        items=[
            TaskCountByDeploymentResponse.model_validate(item)
            for item in _management(services).task_counts_by_deployment(workspace_id)
        ]
    )


@router.get(
    "/api/v1/tasks/aggregate-by-time-window",
    response_model=TaskTimeWindowBucketListResponse,
    operation_id="aggregate_tasks_by_time_window",
)
def aggregate_tasks_by_time_window(
    window_seconds: int = 3600,
    started_at: int | None = None,
    ended_at: int | None = None,
    app_id: identifier_filter = None,
    stub_id: identifier_filter = None,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> TaskTimeWindowBucketListResponse:
    return TaskTimeWindowBucketListResponse(
        items=[
            TaskTimeWindowBucketResponse.model_validate(item)
            for item in _management(services).aggregate_tasks_by_time_window(
                workspace_id,
                window_seconds=window_seconds,
                started_at=_optional_instant(started_at),
                ended_at=_optional_instant(ended_at),
                app_id=app_id,
                stub_id=stub_id,
            )
        ]
    )


@router.delete(
    "/api/v1/tasks",
    response_model=TaskStopResponse,
    operation_id="stop_tasks",
)
def stop_tasks(
    task_ids: Annotated[list[str], Query(default_factory=list)],
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> TaskStopResponse:
    return TaskStopResponse.model_validate(_management(services).stop_tasks(workspace_id, task_ids))


@router.get(
    "/api/v1/tasks/{task_id}/subscribe",
    response_class=StreamingResponse,
    operation_id="subscribe_task",
)
def subscribe_task(
    task_id: str,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> StreamingResponse:
    _management(services).workspace_task(workspace_id, task_id)

    def events() -> Iterator[str]:
        task = _task_response(services.tasks.get(task_id))
        payload = json.dumps(task.model_dump(mode="json"))
        yield f"event: status\ndata: {payload}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get(
    "/api/v1/tasks/{task_id}/call-graph",
    response_model=FunctionCallGraphResponse,
    operation_id="get_task_call_graph",
)
def task_call_graph(
    task_id: str,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> FunctionCallGraphResponse:
    return FunctionControlService(services).function_call_graph(
        task_id,
        workspace_id=workspace_id,
    )


@router.get(
    "/api/v1/tasks/{task_id}/logs",
    response_model=TaskLogListResponse,
    operation_id="get_task_logs",
)
def task_logs(
    task_id: str,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> TaskLogListResponse:
    _management(services).workspace_task(workspace_id, task_id)
    return TaskLogListResponse(
        logs=[TaskLogEntryResponse.model_validate(item) for item in services.tasks.logs(task_id)]
    )


@router.post(
    "/api/v1/tasks/{task_id}/cancel",
    response_model=TaskResponse,
    operation_id="cancel_task",
)
def cancel_task(
    task_id: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> TaskResponse:
    management = _management(services)
    management.workspace_task(workspace_id, task_id)
    management.stop_tasks(workspace_id, [task_id])
    return _task_response(services.tasks.get(task_id))


@router.post(
    "/api/v1/tasks/{task_id}/rerun",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="rerun_task",
)
def rerun_task(
    task_id: str,
    workspace_id: write_workspace,
    service: TaskRerunService = Depends(task_rerun_service),
) -> TaskResponse:
    return _task_response(service.rerun(workspace_id=workspace_id, task_id=task_id))


@router.get(
    "/api/v1/tasks/{task_id}",
    response_model=TaskDetailResponse,
    operation_id="get_task",
)
def get_task(
    task_id: str,
    workspace_id: read_workspace,
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> TaskDetailResponse:
    return _task_detail_response(
        _management(services).task_detail(
            workspace_id,
            task_id,
            can_write=token_has_scope(token, AuthScope.Write),
        )
    )


def _optional_instant(value: int | None) -> datetime | None:
    """A query parameter's epoch seconds, or nothing when it named none."""
    return None if value is None else datetime.fromtimestamp(value, UTC)
