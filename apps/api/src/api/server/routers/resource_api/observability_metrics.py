from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from observability.container_metrics import container_metrics_timeseries
from observability.stream_state import RedisEventStreamRepository
from shared.deployments import DeploymentKind
from shared.http.observability import (
    AccountActivityMeasure,
    AccountActivityResponse,
    AccountContainerCountsResponse,
    ContainerMetricsTimeseriesResponse,
    TaskLatencyTimeseriesResponse,
)
from shared.realtime.contracts import EventRecordType
from shared.realtime.streams import EventHistoryQuery

from api.server.auth import read_user, read_workspace
from api.server.dependencies import current_services
from api.server.identifiers import identifier_filter
from api.server.routers.resource_api.common import _management, _parsed_time, member_workspaces
from api.server.services import ApiServices

router = APIRouter()


@router.get(
    "/api/v1/metrics/task-latency",
    response_model=TaskLatencyTimeseriesResponse,
    operation_id="get_task_latency_timeseries",
)
def api_v1_task_latency_timeseries(
    stub_id: Annotated[list[str], Query(default_factory=list)],
    app_id: identifier_filter = None,
    workload_name: str | None = None,
    workload_kind: DeploymentKind | None = None,
    deployment_id: identifier_filter = None,
    window_seconds: int = Query(default=3600, ge=1),
    start: str | None = None,
    end: str | None = None,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> TaskLatencyTimeseriesResponse:
    return TaskLatencyTimeseriesResponse.model_validate(
        _management(services).task_latency_timeseries(
            workspace_id,
            stub_ids=tuple(value.strip() for value in stub_id if value.strip()),
            deployment_id=deployment_id,
            app_id=app_id,
            workload_name=workload_name,
            workload_kind=workload_kind,
            window_seconds=window_seconds,
            start=_parsed_time(start),
            end=_parsed_time(end),
        )
    )


@router.get(
    "/api/v1/metrics/account/containers",
    response_model=AccountContainerCountsResponse,
    operation_id="get_account_container_counts",
)
def api_v1_account_container_counts(
    user_id: read_user,
    services: ApiServices = Depends(current_services),
) -> AccountContainerCountsResponse:
    """What the signed-in account is holding right now.

    Account-scoped for the reason the billing summary is: the concurrency
    ceiling is a term of a plan and a plan belongs to a payer, so somebody
    running dev, staging and prod reads one figure against one limit rather than
    adding up their own.
    """

    return AccountContainerCountsResponse.model_validate(
        _management(services).account_container_counts(
            workspace_ids=list(member_workspaces(services, user_id))
        )
    )


@router.get(
    "/api/v1/metrics/account/activity",
    response_model=AccountActivityResponse,
    operation_id="get_account_activity",
)
def api_v1_account_activity(
    measure: AccountActivityMeasure = AccountActivityMeasure.Containers,
    window_seconds: int = Query(default=3600, ge=60),
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(default=5, ge=1, le=20),
    *,
    user_id: read_user,
    services: ApiServices = Depends(current_services),
) -> AccountActivityResponse:
    """What the account started, or held, over a window — split by app.

    Scoped to the workspaces this person is a member of, resolved from the
    membership rows naming them rather than from anything the request supplied:
    an account-wide reading assembled from ids a caller named would be a reading
    of whatever it asked for.
    """

    return AccountActivityResponse.model_validate(
        _management(services).account_activity(
            workspaces=member_workspaces(services, user_id),
            measure=measure,
            window_seconds=window_seconds,
            start=_parsed_time(start),
            end=_parsed_time(end),
            limit=limit,
        )
    )


@router.get(
    "/api/v1/metrics/containers/{container_id}/timeseries",
    response_model=ContainerMetricsTimeseriesResponse,
    operation_id="get_container_metrics_timeseries",
)
def api_v1_container_metrics_timeseries(
    container_id: str,
    limit: int = Query(default=500, ge=1, le=2000),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> ContainerMetricsTimeseriesResponse:
    container = _management(services).get_container(container_id, workspace=workspace_id)
    query = EventHistoryQuery(
        workspace_id=container.workspace_id or "",
        stub_id=container.stub_id or "",
        container_id=container_id,
        event_types=(EventRecordType.ContainerMetrics,),
        limit=limit,
    )
    records = RedisEventStreamRepository(services.redis()).read_event_history(query)
    return container_metrics_timeseries(container_id, records)
