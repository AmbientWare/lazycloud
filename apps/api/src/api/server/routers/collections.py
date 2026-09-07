from __future__ import annotations

from execution.collections.redis import (
    RedisMapService,
    RedisSimpleQueueService,
)
from fastapi import APIRouter, Depends, Query, Response, status
from shared.http.collections import (
    MapCollectionInfo,
    MapCollectionListResponse,
    MapCountResponse,
    MapDeleteResponse,
    MapGetResponse,
    MapKeyBody,
    MapKeysResponse,
    MapSetBody,
    MapSetResponse,
    SimpleQueueEmptyResponse,
    SimpleQueueInfo,
    SimpleQueueListResponse,
    SimpleQueuePeekResponse,
    SimpleQueuePopResponse,
    SimpleQueuePutBody,
    SimpleQueuePutResponse,
    SimpleQueueSizeResponse,
    encode_bytes,
)

from api.server.auth import read_workspace, write_workspace
from api.server.service_dependencies import map_service, simple_queue_service

router = APIRouter(tags=["collections"])


@router.get(
    "/api/v1/simplequeues",
    response_model=SimpleQueueListResponse,
    operation_id="list_simple_queues",
)
def list_simple_queues(
    workspace_id: read_workspace,
    service: RedisSimpleQueueService = Depends(simple_queue_service),
) -> SimpleQueueListResponse:
    return SimpleQueueListResponse(
        queues=[
            SimpleQueueInfo(
                name=stats.name,
                size=stats.size,
                oldest_message_age_seconds=stats.oldest_message_age_seconds,
                put_rate_per_minute=stats.put_rate_per_minute,
            )
            for name in service.simple_queue_names(workspace_id)
            for stats in (service.simple_queue_stats(workspace_id, name),)
        ]
    )


@router.get(
    "/api/v1/maps",
    response_model=MapCollectionListResponse,
    operation_id="list_map_collections",
)
def list_map_collections(
    workspace_id: read_workspace,
    service: RedisMapService = Depends(map_service),
) -> MapCollectionListResponse:
    return MapCollectionListResponse(
        maps=[
            MapCollectionInfo(
                name=stats.name,
                count=stats.count,
                size_bytes=stats.size_bytes,
                expiring_keys=stats.expiring_keys,
                nearest_expiry_seconds=stats.nearest_expiry_seconds,
            )
            for name in service.map_names(workspace_id)
            for stats in (service.map_stats(workspace_id, name),)
        ]
    )


@router.post(
    "/api/v1/maps/{name:path}/set",
    response_model=MapSetResponse,
    operation_id="set_map_value",
)
def map_set(
    name: str,
    request: MapSetBody,
    workspace_id: write_workspace,
    service: RedisMapService = Depends(map_service),
) -> MapSetResponse:
    service.map_set(
        workspace_id,
        name,
        request.key,
        request.bytes_value(),
        ttl_seconds=request.ttl_seconds,
    )
    return MapSetResponse()


@router.get(
    "/api/v1/maps/{name:path}/get",
    response_model=MapGetResponse,
    operation_id="get_map_value",
)
def map_get(
    name: str,
    key: str = Query(),
    *,
    workspace_id: read_workspace,
    service: RedisMapService = Depends(map_service),
) -> MapGetResponse:
    value = service.map_get(workspace_id, name, key)
    return MapGetResponse(value_base64=encode_bytes(value))


@router.post(
    "/api/v1/maps/{name:path}/delete",
    response_model=MapDeleteResponse,
    operation_id="delete_map_value",
)
def map_delete(
    name: str,
    request: MapKeyBody,
    workspace_id: write_workspace,
    service: RedisMapService = Depends(map_service),
) -> MapDeleteResponse:
    service.map_delete(workspace_id, name, request.key)
    return MapDeleteResponse()


@router.get(
    "/api/v1/maps/{name:path}/count",
    response_model=MapCountResponse,
    operation_id="count_map_values",
)
def map_count(
    name: str,
    workspace_id: read_workspace,
    service: RedisMapService = Depends(map_service),
) -> MapCountResponse:
    return MapCountResponse(count=service.map_count(workspace_id, name))


@router.get(
    "/api/v1/maps/{name:path}/keys",
    response_model=MapKeysResponse,
    operation_id="list_map_keys",
)
def map_keys(
    name: str,
    workspace_id: read_workspace,
    service: RedisMapService = Depends(map_service),
) -> MapKeysResponse:
    return MapKeysResponse(keys=list(service.map_keys(workspace_id, name)))


@router.delete(
    "/api/v1/maps/{name:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_map_collection",
)
def delete_map_collection(
    name: str,
    workspace_id: write_workspace,
    service: RedisMapService = Depends(map_service),
) -> None:
    service.delete_map(workspace_id, name)


@router.post(
    "/api/v1/simplequeues/{name:path}/put",
    response_model=SimpleQueuePutResponse,
    operation_id="put_simple_queue_value",
)
def simple_queue_put(
    name: str,
    request: SimpleQueuePutBody,
    workspace_id: write_workspace,
    service: RedisSimpleQueueService = Depends(simple_queue_service),
) -> SimpleQueuePutResponse:
    service.simple_queue_put(workspace_id, name, request.bytes_value())
    return SimpleQueuePutResponse()


@router.post(
    "/api/v1/simplequeues/{name:path}/pop",
    response_model=SimpleQueuePopResponse,
    operation_id="pop_simple_queue_value",
)
def simple_queue_pop(
    name: str,
    workspace_id: write_workspace,
    service: RedisSimpleQueueService = Depends(simple_queue_service),
) -> SimpleQueuePopResponse:
    value = service.simple_queue_pop(workspace_id, name)
    return SimpleQueuePopResponse(value_base64=encode_bytes(value))


@router.get(
    "/api/v1/simplequeues/{name:path}/peek",
    response_model=SimpleQueuePeekResponse,
    operation_id="peek_simple_queue_value",
)
def simple_queue_peek(
    name: str,
    workspace_id: read_workspace,
    service: RedisSimpleQueueService = Depends(simple_queue_service),
) -> SimpleQueuePeekResponse:
    value = service.simple_queue_peek(workspace_id, name)
    return SimpleQueuePeekResponse(value_base64=encode_bytes(value))


@router.get(
    "/api/v1/simplequeues/{name:path}/empty",
    response_model=SimpleQueueEmptyResponse,
    operation_id="simple_queue_empty",
)
def simple_queue_empty(
    name: str,
    workspace_id: read_workspace,
    service: RedisSimpleQueueService = Depends(simple_queue_service),
) -> SimpleQueueEmptyResponse:
    return SimpleQueueEmptyResponse(empty=service.simple_queue_empty(workspace_id, name))


@router.get(
    "/api/v1/simplequeues/{name:path}/size",
    response_model=SimpleQueueSizeResponse,
    operation_id="get_simple_queue_size",
)
def simple_queue_size(
    name: str,
    workspace_id: read_workspace,
    service: RedisSimpleQueueService = Depends(simple_queue_service),
) -> SimpleQueueSizeResponse:
    return SimpleQueueSizeResponse(size=service.simple_queue_size(workspace_id, name))


@router.delete(
    "/api/v1/simplequeues/{name:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_simple_queue",
)
def delete_simple_queue(
    name: str,
    workspace_id: write_workspace,
    service: RedisSimpleQueueService = Depends(simple_queue_service),
) -> None:
    service.delete_queue(workspace_id, name)
