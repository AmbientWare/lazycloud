from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from observability.container_metrics import container_metrics_timeseries
from observability.stream_state import RedisEventStreamRepository
from shared.errors import InvalidInputError
from shared.http.observability import (
    ContainerMetricsTimeseriesResponse,
    TaskLatencyTimeseriesResponse,
    WorkspaceActivityMeasure,
    WorkspaceActivityResponse,
    WorkspaceContainerCountsResponse,
)
from shared.realtime.contracts import EventRecordType
from shared.realtime.streams import EventHistoryQuery

from api.server.auth import read_workspace
from api.server.dependencies import current_services
from api.server.identifiers import identifier_filter
from api.server.routers.resource_api.common import _management
from api.server.services import ApiServices

router = APIRouter()


@router.get(
    "/api/v1/metrics/task-latency",
    response_model=TaskLatencyTimeseriesResponse,
    operation_id="get_task_latency_timeseries",
)
def api_v1_task_latency_timeseries(
    stub_id: Annotated[list[str], Query()],
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
            window_seconds=window_seconds,
            start=_parse_time(start),
            end=_parse_time(end),
        )
    )


@router.get(
    "/api/v1/metrics/workspace/containers",
    response_model=WorkspaceContainerCountsResponse,
    operation_id="get_workspace_container_counts",
)
def api_v1_workspace_container_counts(
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> WorkspaceContainerCountsResponse:
    return WorkspaceContainerCountsResponse.model_validate(
        _management(services).workspace_container_counts(workspace_id)
    )


@router.get(
    "/api/v1/metrics/workspace/activity",
    response_model=WorkspaceActivityResponse,
    operation_id="get_workspace_activity",
)
def api_v1_workspace_activity(
    measure: WorkspaceActivityMeasure = WorkspaceActivityMeasure.Containers,
    window_seconds: int = Query(default=3600, ge=60),
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(default=5, ge=1, le=20),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> WorkspaceActivityResponse:
    return WorkspaceActivityResponse.model_validate(
        _management(services).workspace_activity(
            workspace_id,
            measure=measure,
            window_seconds=window_seconds,
            start=_parse_time(start),
            end=_parse_time(end),
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


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        msg = f"invalid timestamp: {value}"
        raise InvalidInputError(msg) from exc
