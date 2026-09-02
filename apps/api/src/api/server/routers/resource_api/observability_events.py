from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

from coordination.stream_tail import RedisStreamTailBroker
from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from observability.event_summary import (
    ContainerEventsBatchRequest,
    ContainerEventsBatchResponse,
    ContainerEventsBatchTarget,
    ContainerEventSummary,
)
from observability.stream_state import (
    AsyncRedisEventStreamRepository,
    RedisEventStreamRepository,
    RedisStreamRecord,
)
from observability.workspace_changes import (
    WORKSPACE_CHANGE_SSE_EVENT,
    AsyncWorkspaceChangeReader,
    WorkspaceChangeRecord,
)
from shared.http.observability import EventListResponse, EventQueryResponse
from shared.realtime.streams import EventHistoryQuery

from api.server.auth import read_workspace
from api.server.dependencies import current_services
from api.server.routers.resource_api.common import _management
from api.server.services import ApiServices
from api.server.sse import (
    SSE_HEARTBEAT_SECONDS,
    SseItem,
    sse_response_items,
    sse_response_prepared,
)

router = APIRouter()


@router.get(
    "/api/v1/events/changes/stream",
    response_class=StreamingResponse,
    operation_id="stream_workspace_changes",
)
def api_v1_stream_workspace_changes(
    max_events: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> StreamingResponse:
    changes = AsyncWorkspaceChangeReader(services.require_async_io().realtime)
    return sse_response_prepared(
        lambda: _prepare_workspace_change_items(
            changes,
            workspace_id=workspace_id,
            after=last_event_id,
            max_events=max_events,
        )
    )


@router.get("/api/v1/events", response_model=EventListResponse, operation_id="list_events")
def list_events(
    limit: int = Query(100, ge=1, le=1000),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> EventListResponse:
    return EventListResponse(
        events=services.events.list(
            workspace_id=workspace_id,
            limit=limit,
        )
    )


async def _prepare_workspace_change_items(
    changes: AsyncWorkspaceChangeReader,
    *,
    workspace_id: str,
    after: str | None,
    max_events: int,
) -> AsyncIterator[SseItem]:
    return _workspace_change_items(
        await changes.follow(
            workspace_id,
            after=after,
            max_events=max_events,
            heartbeat_seconds=SSE_HEARTBEAT_SECONDS,
        )
    )


async def _workspace_change_items(
    records: AsyncIterator[WorkspaceChangeRecord | None],
) -> AsyncIterator[SseItem]:
    async for record in records:
        if record is None:
            yield None
        else:
            yield (WORKSPACE_CHANGE_SSE_EVENT, record.entry_id, record.event)


@router.get(
    "/api/v1/events/containers/{container_id}/stream",
    response_class=StreamingResponse,
)
def api_v1_stream_container_events(
    container_id: str,
    follow: bool = False,
    cursor: str | None = None,
    clamp: bool | None = None,
    max_events: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> StreamingResponse:
    query = EventHistoryQuery(workspace_id=workspace_id, container_id=container_id)
    if response := _redis_event_response(
        services,
        query,
        follow=follow,
        max_events=max_events,
        last_event_id=last_event_id or cursor,
        clamp=clamp,
    ):
        return response
    result = _management(services).event_history(workspace_id, container_id=container_id)
    return sse_response_items((item.action, item.id, item) for item in result.data)


@router.get(
    "/api/v1/events/containers/{container_id}/summary",
    response_model=ContainerEventSummary | None,
    operation_id="get_container_event_summary",
)
def api_v1_container_event_summary(
    container_id: str,
    include_events: bool = False,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> ContainerEventSummary | None:
    request = ContainerEventsBatchRequest(
        targets=[ContainerEventsBatchTarget(container_id=container_id)],
        include_events=include_events,
    )
    response = services.events.container_events_batch(request, workspace_id=workspace_id)
    return response.items[0] if response.items else None


@router.get(
    "/api/v1/events/containers/{container_id}",
    response_model=EventQueryResponse,
    operation_id="get_container_events",
)
def api_v1_get_container_events(
    container_id: str,
    limit: int = 100,
    cursor: str | None = None,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> EventQueryResponse:
    return _management(services).event_history(
        workspace_id,
        container_id=container_id,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/api/v1/events/stubs/{stub_id}/containers/{container_id}/stream",
    response_class=StreamingResponse,
)
def api_v1_stream_stub_container_events(
    stub_id: str,
    container_id: str,
    follow: bool = False,
    cursor: str | None = None,
    clamp: bool | None = None,
    max_events: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> StreamingResponse:
    query = EventHistoryQuery(
        workspace_id=workspace_id,
        stub_id=stub_id,
        container_id=container_id,
    )
    if response := _redis_event_response(
        services,
        query,
        follow=follow,
        max_events=max_events,
        last_event_id=last_event_id or cursor,
        clamp=clamp,
    ):
        return response
    result = _management(services).event_history(workspace_id, container_id=container_id)
    return sse_response_items(
        (item.action, item.id, item)
        for item in result.data
        if item.data.get("stub_id") in {None, stub_id}
    )


@router.get(
    "/api/v1/events/stubs/{stub_id}/containers/{container_id}/summary",
    response_model=ContainerEventSummary | None,
    operation_id="get_stub_container_event_summary",
)
def api_v1_stub_container_event_summary(
    stub_id: str,
    container_id: str,
    include_events: bool = False,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> ContainerEventSummary | None:
    request = ContainerEventsBatchRequest(
        targets=[ContainerEventsBatchTarget(container_id=container_id, stub_id=stub_id)],
        include_events=include_events,
    )
    response = services.events.container_events_batch(request, workspace_id=workspace_id)
    return response.items[0] if response.items else None


@router.get(
    "/api/v1/events/stubs/{stub_id}/containers/{container_id}",
    response_model=EventQueryResponse,
    operation_id="get_stub_container_events",
)
def api_v1_get_stub_container_events(
    stub_id: str,
    container_id: str,
    limit: int = 100,
    cursor: str | None = None,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> EventQueryResponse:
    result = _management(services).event_history(
        workspace_id,
        container_id=container_id,
        limit=limit,
        cursor=cursor,
    )
    return result.model_copy(
        update={
            "data": tuple(
                item for item in result.data if item.data.get("stub_id") in {None, stub_id}
            )
        }
    )


@router.get(
    "/api/v1/events/stubs/{stub_id}/stream",
    response_class=StreamingResponse,
)
def api_v1_stream_stub_events(
    stub_id: str,
    follow: bool = False,
    cursor: str | None = None,
    clamp: bool | None = None,
    max_events: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> StreamingResponse:
    query = EventHistoryQuery(workspace_id=workspace_id, stub_id=stub_id)
    if response := _redis_event_response(
        services,
        query,
        follow=follow,
        max_events=max_events,
        last_event_id=last_event_id or cursor,
        clamp=clamp,
    ):
        return response
    result = _management(services).event_history(
        workspace_id, resource_type="stub", resource_id=stub_id
    )
    return sse_response_items((item.action, item.id, item) for item in result.data)


@router.get(
    "/api/v1/events/tasks/{task_id}/stream",
    response_class=StreamingResponse,
)
def api_v1_stream_task_events(
    task_id: str,
    follow: bool = False,
    cursor: str | None = None,
    clamp: bool | None = None,
    max_events: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> StreamingResponse:
    query = EventHistoryQuery(workspace_id=workspace_id, task_id=task_id)
    if response := _redis_event_response(
        services,
        query,
        follow=follow,
        max_events=max_events,
        last_event_id=last_event_id or cursor,
        clamp=clamp,
    ):
        return response
    result = _management(services).event_history(workspace_id, task_id=task_id)
    return sse_response_items((item.action, item.id, item) for item in result.data)


@router.get(
    "/api/v1/events/apps/{app_id}/stream",
    response_class=StreamingResponse,
)
def api_v1_stream_app_events(
    app_id: str,
    follow: bool = False,
    cursor: str | None = None,
    clamp: bool | None = None,
    max_events: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> StreamingResponse:
    query = EventHistoryQuery(workspace_id=workspace_id, app_id=app_id)
    if response := _redis_event_response(
        services,
        query,
        follow=follow,
        max_events=max_events,
        last_event_id=last_event_id or cursor,
        clamp=clamp,
    ):
        return response
    result = _management(services).event_history(
        workspace_id,
        resource_type="app",
        resource_id=app_id,
    )
    return sse_response_items((item.action, item.id, item) for item in result.data)


@router.get(
    "/api/v1/events/history",
    response_model=EventQueryResponse,
    operation_id="get_event_history",
)
def api_v1_event_history(
    task_id: str | None = None,
    container_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> EventQueryResponse:
    return _management(services).event_history(
        workspace_id,
        task_id=task_id,
        container_id=container_id,
        resource_type=resource_type,
        resource_id=resource_id,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/api/v1/events/stream",
    response_class=StreamingResponse,
)
def api_v1_stream_workspace_events(
    follow: bool = False,
    cursor: str | None = None,
    clamp: bool | None = None,
    max_events: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> StreamingResponse:
    query = EventHistoryQuery(workspace_id=workspace_id)
    if response := _redis_event_response(
        services,
        query,
        follow=follow,
        max_events=max_events,
        last_event_id=last_event_id or cursor,
        clamp=clamp,
    ):
        return response
    result = _management(services).event_history(workspace_id)
    return sse_response_items((item.action, item.id, item) for item in result.data)


@router.post(
    "/api/v1/events/containers/batch",
    response_model=ContainerEventsBatchResponse,
    operation_id="get_container_events_batch",
)
def api_v1_container_events_batch(
    request: ContainerEventsBatchRequest,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> ContainerEventsBatchResponse:
    return services.events.container_events_batch(request, workspace_id=workspace_id)


def _redis_event_response(
    services: ApiServices,
    query: EventHistoryQuery,
    *,
    follow: bool,
    max_events: int,
    last_event_id: str | None,
    clamp: bool | None,
) -> StreamingResponse | None:
    if follow:
        async_io = services.require_async_io()
        repository = AsyncRedisEventStreamRepository(async_io.redis)
        return sse_response_prepared(
            lambda: _prepare_redis_event_items(
                repository,
                async_io.realtime,
                query,
                last_event_id=last_event_id,
                clamp=clamp,
                max_events=max_events,
            )
        )
    repository = RedisEventStreamRepository(services.redis())
    try:
        records = repository.read_event_history(query, cursor=last_event_id, clamp=clamp)
    except (AttributeError, TypeError, RuntimeError):
        return None
    if not records and last_event_id is not None:
        return sse_response_items(())
    if not records:
        return None
    return sse_response_items(_redis_event_items(records))


async def _prepare_redis_event_items(
    repository: AsyncRedisEventStreamRepository,
    tail: RedisStreamTailBroker,
    query: EventHistoryQuery,
    *,
    last_event_id: str | None,
    clamp: bool | None,
    max_events: int,
) -> AsyncIterator[SseItem]:
    return _async_redis_event_items(
        await repository.follow_event_history(
            tail,
            query,
            last_event_id=last_event_id,
            clamp=clamp,
            max_events=max_events,
            heartbeat_seconds=SSE_HEARTBEAT_SECONDS,
        )
    )


def _redis_event_items(
    records: Iterator[RedisStreamRecord] | tuple[RedisStreamRecord, ...],
) -> Iterator[tuple[str, str, object]]:
    for record in records:
        yield (record.event_name, record.entry_id, record.body)


async def _async_redis_event_items(
    records: AsyncIterator[RedisStreamRecord | None],
) -> AsyncIterator[SseItem]:
    async for record in records:
        if record is None:
            yield None
        else:
            yield (record.event_name, record.entry_id, record.body)


@router.get(
    "/api/v1/events/tasks/{task_id}",
    response_model=EventQueryResponse,
    operation_id="get_task_events",
)
def api_v1_get_task_events(
    task_id: str,
    limit: int = 100,
    cursor: str | None = None,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> EventQueryResponse:
    return _management(services).event_history(
        workspace_id,
        task_id=task_id,
        limit=limit,
        cursor=cursor,
    )
