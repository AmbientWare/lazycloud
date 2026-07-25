from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime
from typing import TypedDict

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from observability.stream_state import (
    RedisEventStreamRepository,
    RedisStreamRecord,
    log_record_from_redis,
)
from shared.errors import NotFoundError
from shared.http.observability import LogObjectType, LogQueryRequest, LogQueryResponse
from shared.realtime.streams import EventStreamPlanner, LogStreamQuery
from shared.tasks import is_terminal_task_status

from api.server.auth import read_workspace
from api.server.dependencies import current_services
from api.server.routers.resource_api.common import _management, _parsed_time
from api.server.services import ApiServices
from api.server.sse import sse_response

router = APIRouter()


@router.get("/api/v1/logs/stream", response_class=StreamingResponse)
def api_v1_stream_logs(
    object_id: str | None = None,
    object_type: LogObjectType | None = None,
    stub_id: str | None = None,
    app_id: str | None = None,
    task_id: str | None = None,
    container_id: str | None = None,
    machine_id: str | None = None,
    worker_id: str | None = None,
    query: str | None = None,
    limit: int = 100,
    page: int = 0,
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
        task_id=task_id,
        container_id=container_id,
        machine_id=machine_id,
        worker_id=worker_id,
        query=query,
        limit=limit,
        page=page,
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
    if response := _redis_log_response(
        services,
        stream_query,
        follow=follow,
        max_events=max_events,
        wait_seconds=_effective_wait_seconds(request, wait_seconds),
        last_event_id=last_event_id,
    ):
        return response
    if _requires_stream_metadata(stream_query):
        return sse_response(())
    if request.cursor:
        return sse_response(())
    if follow and request.task_id:
        return sse_response(
            _follow_task_logs(
                services,
                request,
                poll_interval_seconds=poll_interval_seconds,
            )
        )
    result = _management(services).logs(request.workspace_id, **_management_log_kwargs(request))
    return sse_response(("log", item.id, item) for item in result.data)


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
    task_id: str | None = None,
    container_id: str | None = None,
    machine_id: str | None = None,
    worker_id: str | None = None,
    query: str | None = None,
    limit: int = 100,
    page: int = 0,
    start_time: str | None = None,
    end_time: str | None = None,
    cursor: str | None = None,
    seq_num: int | None = Query(None, ge=0),
    wait: int | None = Query(None, ge=0, le=30),
    clamp: bool | None = None,
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
        task_id=task_id,
        container_id=container_id,
        machine_id=machine_id,
        worker_id=worker_id,
        query=query,
        limit=limit,
        page=page,
        start_time=_parsed_time(start_time),
        end_time=_parsed_time(end_time),
        cursor=cursor,
        seq_num=seq_num,
        wait=wait,
        clamp=clamp,
    )
    request = _resolve_log_query_request(services, request)
    stream_query = _log_stream_query(request)
    if response := _redis_log_query_response(services, stream_query):
        return response
    if _requires_stream_metadata(stream_query):
        return _empty_log_query_response(stream_query)
    if request.cursor:
        return _empty_log_query_response(stream_query)
    return _management(services).logs(request.workspace_id, **_management_log_kwargs(request))


def _follow_task_logs(
    services: ApiServices,
    request: LogQueryRequest,
    *,
    poll_interval_seconds: float,
) -> Iterator[tuple[str, str, object]]:
    seen: set[str] = set()
    sleep_seconds = max(poll_interval_seconds, 0.05)
    while True:
        log_kwargs = _management_log_kwargs(request)
        log_kwargs["page"] = 0
        result = _management(services).logs(request.workspace_id, **log_kwargs)
        for item in result.data:
            if item.id in seen:
                continue
            seen.add(item.id)
            yield ("log", item.id, item)
        try:
            task = services.tasks.get(request.task_id or "")
        except NotFoundError:
            return
        if is_terminal_task_status(task.status):
            return
        time.sleep(sleep_seconds)


def _log_query_request(
    *,
    workspace_id: str,
    object_id: str | None = None,
    object_type: LogObjectType | None = None,
    stub_id: str | None = None,
    app_id: str | None = None,
    task_id: str | None = None,
    container_id: str | None = None,
    machine_id: str | None = None,
    worker_id: str | None = None,
    query: str | None = None,
    limit: int = 100,
    page: int = 0,
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
        task_id=task_id,
        container_id=container_id,
        machine_id=machine_id,
        worker_id=worker_id,
        query=query,
        limit=limit,
        page=page,
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
        task_id=request.task_id or "",
        container_id=request.container_id or "",
        machine_id=request.machine_id or "",
        worker_id=request.worker_id or "",
        query=request.query or "",
        limit=request.limit,
        page=request.page,
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
            "stub_id": deployment.stub_id,
            "app_id": deployment.app_id,
        }
    )


def _management_log_kwargs(request: LogQueryRequest) -> _ManagementLogKwargs:
    return {
        "object_id": request.object_id,
        "object_type": request.object_type.value if request.object_type is not None else None,
        "stub_id": request.stub_id,
        "app_id": request.app_id,
        "task_id": request.task_id,
        "container_id": request.container_id,
        "machine_id": request.machine_id,
        "worker_id": request.worker_id,
        "query": request.query,
        "limit": request.limit,
        "page": request.page,
        "start_time": request.start_time,
        "end_time": request.end_time,
        "seq_num": request.seq_num,
        "wait_seconds": request.wait,
        "clamp": request.clamp,
    }


class _ManagementLogKwargs(TypedDict):
    object_id: str | None
    object_type: str | None
    stub_id: str | None
    app_id: str | None
    task_id: str | None
    container_id: str | None
    machine_id: str | None
    worker_id: str | None
    query: str | None
    limit: int
    page: int
    start_time: datetime | None
    end_time: datetime | None
    seq_num: int | None
    wait_seconds: float | None
    clamp: bool | None


def _effective_wait_seconds(
    request: LogQueryRequest,
    wait_seconds: float | None,
) -> float:
    if request.wait is not None:
        return float(request.wait)
    if wait_seconds is not None:
        return wait_seconds
    return 1.0


def _redis_log_query_response(
    services: ApiServices,
    query: LogStreamQuery,
) -> LogQueryResponse | None:
    repository = RedisEventStreamRepository(services.redis())
    try:
        records = repository.read_logs(query)
    except (AttributeError, TypeError, RuntimeError):
        return None
    if not records:
        return None
    logs = tuple(log_record_from_redis(record) for record in records)
    return LogQueryResponse(
        object_id=query.object_id,
        object_type=_log_object_type(query.object_type),
        data=logs,
        count=len(logs),
        total_expected=len(logs),
        streams=repository.planner.plan_log_page(query).streams,
    )


def _empty_log_query_response(query: LogStreamQuery) -> LogQueryResponse:
    return LogQueryResponse(
        object_id=query.object_id,
        object_type=_log_object_type(query.object_type),
        streams=EventStreamPlanner().plan_log_page(query).streams,
    )


def _log_object_type(value: str) -> LogObjectType | None:
    return LogObjectType(value) if value else None


def _requires_stream_metadata(query: LogStreamQuery) -> bool:
    if query.stub_id or query.app_id or query.machine_id or query.worker_id:
        return True
    return query.object_type in {"deployment", "stub", "app", "machine"}


def _redis_log_response(
    services: ApiServices,
    query: LogStreamQuery,
    *,
    follow: bool,
    max_events: int,
    wait_seconds: float,
    last_event_id: str | None,
) -> StreamingResponse | None:
    repository = RedisEventStreamRepository(services.redis())
    if follow:
        return sse_response(
            _redis_log_items(
                repository.stream_logs(
                    query,
                    last_event_id=last_event_id,
                    block_milliseconds=int(wait_seconds * 1000),
                    max_events=max_events,
                )
            )
        )
    try:
        records = repository.read_logs(query)
    except (AttributeError, TypeError, RuntimeError):
        return None
    if not records:
        return None
    return sse_response(_redis_log_items(records))


def _redis_log_items(
    records: Iterator[RedisStreamRecord] | tuple[RedisStreamRecord, ...],
) -> Iterator[tuple[str, str, object]]:
    for record in records:
        log_record = log_record_from_redis(record)
        yield ("log", record.entry_id, log_record)
