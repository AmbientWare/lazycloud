from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import TypedDict

from coordination.stream_tail import RedisStreamTailBroker
from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from observability.stream_state import (
    AsyncRedisEventStreamRepository,
    RedisStreamRecord,
    log_record_from_redis,
)
from shared.http.observability import LogObjectType, LogQueryRequest, LogQueryResponse
from shared.realtime.streams import LogStreamQuery

from api.server.auth import read_workspace
from api.server.dependencies import current_services
from api.server.routers.resource_api.common import _management, _parsed_time
from api.server.services import ApiServices
from api.server.sse import (
    SSE_HEARTBEAT_SECONDS,
    SseItem,
    sse_response_items,
    sse_response_prepared,
)

router = APIRouter()


@router.get("/api/v1/logs/stream", response_class=StreamingResponse)
def api_v1_stream_logs(
    object_id: str | None = None,
    object_type: LogObjectType | None = None,
    stub_id: str | None = None,
    app_id: str | None = None,
    deployment_id: str | None = None,
    task_id: str | None = None,
    container_id: str | None = None,
    machine_id: str | None = None,
    worker_id: str | None = None,
    query: str | None = None,
    limit: int = Query(100, gt=0, le=1_000),
    start_time: str | None = None,
    end_time: str | None = None,
    cursor: str | None = None,
    seq_num: int | None = Query(None, ge=0),
    wait: int | None = Query(None, ge=0, le=30),
    clamp: bool | None = None,
    follow: bool = False,
    max_events: int = Query(0, ge=0),
    wait_seconds: float | None = Query(None, ge=0, le=30),
    poll_interval_seconds: float = 0.25,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> StreamingResponse:
    request = _log_query_request(
        workspace_id=workspace_id,
        object_id=object_id,
        object_type=object_type,
        stub_id=stub_id,
        app_id=app_id,
        deployment_id=deployment_id,
        task_id=task_id,
        container_id=container_id,
        machine_id=machine_id,
        worker_id=worker_id,
        query=query,
        limit=limit,
        start_time=_parsed_time(start_time),
        end_time=_parsed_time(end_time),
        cursor=cursor,
        seq_num=seq_num,
        wait=wait,
        clamp=clamp,
        last_event_id=last_event_id,
    )
    request = _resolve_log_query_request(services, request)
    stream_query = _log_stream_query(request, wait_seconds=wait_seconds)
    if follow:
        return _redis_log_response(
            services,
            stream_query,
            max_events=max_events,
            last_event_id=last_event_id,
        )
    result = _management(services).logs(request.workspace_id, **_management_log_kwargs(request))
    return sse_response_items(("log", item.cursor, item) for item in result.data)


@router.get(
    "/api/v1/logs",
    response_model=LogQueryResponse,
    operation_id="get_logs",
)
def api_v1_get_logs(
    object_id: str | None = None,
    object_type: LogObjectType | None = None,
    stub_id: str | None = None,
    app_id: str | None = None,
    deployment_id: str | None = None,
    task_id: str | None = None,
    container_id: str | None = None,
    machine_id: str | None = None,
    worker_id: str | None = None,
    query: str | None = None,
    limit: int = Query(100, gt=0, le=1_000),
    start_time: str | None = None,
    end_time: str | None = None,
    cursor: str | None = None,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> LogQueryResponse:
    request = _log_query_request(
        workspace_id=workspace_id,
        object_id=object_id,
        object_type=object_type,
        stub_id=stub_id,
        app_id=app_id,
        deployment_id=deployment_id,
        task_id=task_id,
        container_id=container_id,
        machine_id=machine_id,
        worker_id=worker_id,
        query=query,
        limit=limit,
        start_time=_parsed_time(start_time),
        end_time=_parsed_time(end_time),
        cursor=cursor,
    )
    request = _resolve_log_query_request(services, request)
    return _management(services).logs(request.workspace_id, **_management_log_kwargs(request))


def _log_query_request(
    *,
    workspace_id: str,
    object_id: str | None = None,
    object_type: LogObjectType | None = None,
    stub_id: str | None = None,
    app_id: str | None = None,
    deployment_id: str | None = None,
    task_id: str | None = None,
    container_id: str | None = None,
    machine_id: str | None = None,
    worker_id: str | None = None,
    query: str | None = None,
    limit: int = 100,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    cursor: str | None = None,
    seq_num: int | None = None,
    wait: int | None = None,
    clamp: bool | None = None,
    last_event_id: str | None = None,
) -> LogQueryRequest:
    if last_event_id and seq_num is None and cursor is None:
        cursor = last_event_id
        start_time = None
    if (cursor is not None or seq_num is not None or start_time is not None) and clamp is None:
        clamp = True
    return LogQueryRequest(
        workspace_id=workspace_id,
        object_id=object_id,
        object_type=object_type,
        stub_id=stub_id,
        app_id=app_id,
        deployment_id=deployment_id,
        task_id=task_id,
        container_id=container_id,
        machine_id=machine_id,
        worker_id=worker_id,
        query=query,
        limit=limit,
        start_time=start_time,
        end_time=end_time,
        cursor=cursor,
        seq_num=seq_num,
        wait=wait,
        clamp=clamp,
    )


def _log_stream_query(
    request: LogQueryRequest,
    *,
    wait_seconds: float | None = None,
) -> LogStreamQuery:
    return LogStreamQuery(
        workspace_id=request.workspace_id,
        object_id=request.object_id or "",
        object_type=request.object_type.value if request.object_type is not None else "",
        stub_id=request.stub_id or "",
        app_id=request.app_id or "",
        deployment_id=request.deployment_id or "",
        task_id=request.task_id or "",
        container_id=request.container_id or "",
        machine_id=request.machine_id or "",
        worker_id=request.worker_id or "",
        query=request.query or "",
        limit=request.limit,
        start_time=request.start_time,
        end_time=request.end_time,
        cursor=request.cursor or "",
        seq_num=request.seq_num,
        wait_seconds=_effective_wait_seconds(request, wait_seconds),
        clamp=request.clamp,
    )


def _resolve_log_query_request(
    services: ApiServices,
    request: LogQueryRequest,
) -> LogQueryRequest:
    if request.object_type is not LogObjectType.Deployment or not request.object_id:
        return request
    deployment = _management(services).retrieve_deployment(
        request.workspace_id,
        request.object_id,
    )
    return request.model_copy(
        update={
            "deployment_id": deployment.id,
            "stub_id": deployment.stub_id,
            "app_id": deployment.app_id,
        }
    )


def _management_log_kwargs(request: LogQueryRequest) -> _ManagementLogKwargs:
    return {
        "stub_id": request.stub_id,
        "app_id": request.app_id,
        "deployment_id": request.deployment_id,
        "task_id": request.task_id,
        "container_id": request.container_id,
        "machine_id": request.machine_id,
        "worker_id": request.worker_id,
        "query": request.query,
        "limit": request.limit,
        "start_time": request.start_time,
        "end_time": request.end_time,
        "cursor": request.cursor,
        "after_cursor": False,
    }


class _ManagementLogKwargs(TypedDict):
    stub_id: str | None
    app_id: str | None
    deployment_id: str | None
    task_id: str | None
    container_id: str | None
    machine_id: str | None
    worker_id: str | None
    query: str | None
    limit: int
    start_time: datetime | None
    end_time: datetime | None
    cursor: str | None
    after_cursor: bool


def _effective_wait_seconds(
    request: LogQueryRequest,
    wait_seconds: float | None,
) -> float:
    if request.wait is not None:
        return float(request.wait)
    if wait_seconds is not None:
        return wait_seconds
    return 1.0


def _redis_log_response(
    services: ApiServices,
    query: LogStreamQuery,
    *,
    max_events: int,
    last_event_id: str | None,
) -> StreamingResponse:
    async_io = services.require_async_io()
    repository = AsyncRedisEventStreamRepository(async_io.redis)
    return sse_response_prepared(
        lambda: _prepare_redis_log_items(
            repository,
            async_io.realtime,
            query,
            last_event_id=last_event_id,
            max_events=max_events,
        )
    )


async def _prepare_redis_log_items(
    repository: AsyncRedisEventStreamRepository,
    tail: RedisStreamTailBroker,
    query: LogStreamQuery,
    *,
    last_event_id: str | None,
    max_events: int,
) -> AsyncIterator[SseItem]:
    return _async_redis_log_items(
        await repository.follow_logs(
            tail,
            query,
            last_event_id=last_event_id,
            max_events=max_events,
            heartbeat_seconds=SSE_HEARTBEAT_SECONDS,
        )
    )


async def _async_redis_log_items(
    records: AsyncIterator[RedisStreamRecord | None],
) -> AsyncIterator[SseItem]:
    async for record in records:
        if record is None:
            yield None
        else:
            yield ("log", record.entry_id, log_record_from_redis(record))
