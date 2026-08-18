from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

import websockets.asyncio.client
from control.service import ControlPlaneService, StubKind, StubRecord
from execution.endpoints.dispatch import (
    EndpointBackendProtocol,
    EndpointResponseStream,
    endpoint_backend_url,
)
from execution.endpoints.service import (
    EndpointIngressDispatchSession,
    EndpointWebSocketDispatchRejected,
)
from fastapi import (
    APIRouter,
    Depends,
    Request,
    Response,
    WebSocket,
    status,
)
from shared.container_requests import CONTAINER_HEALTH_PATH
from shared.http.endpoints import (
    EndpointForwardRequest,
    StartEndpointServeRequest,
    StartEndpointServeResponse,
)
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed
from websockets.typing import Data

from api.server.auth import write_workspace
from api.server.dependencies import (
    authorize_websocket_workspace,
    current_services,
    current_websocket_services,
)
from api.server.deployed_stubs import (
    resolve_deployed_stub,
    resolve_deployed_stub_id,
)
from api.server.http import (
    HOP_BY_HOP_RESPONSE_HEADERS,
    backend_websocket_headers,
    close_websocket,
    forwarded_path,
    forwarded_response,
    request_headers,
    request_query_params,
    websocket_headers,
    websocket_query_params,
    websocket_subprotocols,
)
from api.server.ownership import require_endpoint_stub_workspace
from api.server.service_dependencies import control_plane_service, endpoint_service
from api.server.services import ApiServices, EndpointApiService

router = APIRouter(tags=["endpoint"])
endpoint_router = APIRouter(prefix="/api/v1/endpoints", tags=["endpoint"])
asgi_router = APIRouter(prefix="/api/v1/asgi", tags=["endpoint"])
ENDPOINT_RESOURCE_NAME = "endpoint"
ENDPOINT_METHODS = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"]
ASGI_METHODS = ["CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"]


@endpoint_router.post("/serve", response_model=StartEndpointServeResponse)
def start_endpoint_serve(
    request: StartEndpointServeRequest,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
) -> StartEndpointServeResponse:
    require_endpoint_stub_workspace(control_plane, request.stub_id, workspace_id)
    return service.start_endpoint_serve(request)


@endpoint_router.api_route("/id/{stub_id}", methods=ENDPOINT_METHODS, include_in_schema=False)
async def deployed_endpoint_request_by_id(
    stub_id: str,
    request: Request,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.Endpoint,
        public=False,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    return await _forward_endpoint_request(
        stub,
        service,
        request,
    )


@endpoint_router.api_route("/public/{stub_id}", methods=ENDPOINT_METHODS, include_in_schema=False)
async def deployed_public_endpoint_request_by_id(
    stub_id: str,
    request: Request,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.Endpoint,
        public=True,
        resource_name=ENDPOINT_RESOURCE_NAME,
    )
    return await _forward_endpoint_request(
        stub,
        service,
        request,
    )


@endpoint_router.post("/id/{stub_id}/warmup", response_model=StartEndpointServeResponse)
def deployed_endpoint_warmup_by_id(
    stub_id: str,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> StartEndpointServeResponse:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.Endpoint,
        public=False,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    return _start_deployed_endpoint_serve(stub, service)


@endpoint_router.post(
    "/{deployment_name}/latest/warmup",
    response_model=StartEndpointServeResponse,
)
def deployed_endpoint_warmup_by_latest_path(
    deployment_name: str,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> StartEndpointServeResponse:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Endpoint,
        version=None,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    return _start_deployed_endpoint_serve(stub, service)


@endpoint_router.post(
    "/{deployment_name}/v{version}/warmup",
    response_model=StartEndpointServeResponse,
)
def deployed_endpoint_warmup_by_version(
    deployment_name: str,
    version: int,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> StartEndpointServeResponse:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Endpoint,
        version=version,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    return _start_deployed_endpoint_serve(stub, service)


@endpoint_router.api_route(
    "/{deployment_name}/latest",
    methods=ENDPOINT_METHODS,
    include_in_schema=False,
)
async def deployed_endpoint_request_by_latest_path(
    deployment_name: str,
    request: Request,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Endpoint,
        version=None,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    return await _forward_endpoint_request(
        stub,
        service,
        request,
    )


@endpoint_router.api_route(
    "/{deployment_name}/v{version}",
    methods=ENDPOINT_METHODS,
    include_in_schema=False,
)
async def deployed_endpoint_request_by_version(
    deployment_name: str,
    version: int,
    request: Request,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Endpoint,
        version=version,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    return await _forward_endpoint_request(
        stub,
        service,
        request,
    )


@asgi_router.post("/id/{stub_id}/warmup", response_model=StartEndpointServeResponse)
def deployed_asgi_warmup_by_id(
    stub_id: str,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> StartEndpointServeResponse:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.Asgi,
        public=False,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    return _start_deployed_endpoint_serve(stub, service)


@asgi_router.post(
    "/{deployment_name}/latest/warmup",
    response_model=StartEndpointServeResponse,
)
def deployed_asgi_warmup_by_latest_path(
    deployment_name: str,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> StartEndpointServeResponse:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Asgi,
        version=None,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    return _start_deployed_endpoint_serve(stub, service)


@asgi_router.post(
    "/{deployment_name}/v{version}/warmup",
    response_model=StartEndpointServeResponse,
)
def deployed_asgi_warmup_by_version(
    deployment_name: str,
    version: int,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> StartEndpointServeResponse:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Asgi,
        version=version,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    return _start_deployed_endpoint_serve(stub, service)


@asgi_router.websocket("/id/{stub_id}")
@asgi_router.websocket("/id/{stub_id}/{subpath:path}")
async def deployed_asgi_websocket_by_id(
    websocket: WebSocket,
    stub_id: str,
    subpath: str = "",
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_websocket_services),
) -> None:
    workspace_id = authorize_websocket_workspace(services, websocket)
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.Asgi,
        public=False,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    await _forward_asgi_websocket(
        stub,
        service,
        websocket,
        subpath=subpath,
    )


@asgi_router.websocket("/public/{stub_id}")
@asgi_router.websocket("/public/{stub_id}/{subpath:path}")
async def deployed_public_asgi_websocket_by_id(
    websocket: WebSocket,
    stub_id: str,
    subpath: str = "",
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_websocket_services),
) -> None:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.Asgi,
        public=True,
        resource_name=ENDPOINT_RESOURCE_NAME,
    )
    await _forward_asgi_websocket(
        stub,
        service,
        websocket,
        subpath=subpath,
    )


@asgi_router.websocket("/{deployment_name}/latest")
@asgi_router.websocket("/{deployment_name}/latest/{subpath:path}")
async def deployed_asgi_websocket_by_latest_path(
    websocket: WebSocket,
    deployment_name: str,
    subpath: str = "",
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_websocket_services),
) -> None:
    workspace_id = authorize_websocket_workspace(services, websocket)
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Asgi,
        version=None,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    await _forward_asgi_websocket(
        stub,
        service,
        websocket,
        subpath=subpath,
    )


@asgi_router.websocket("/{deployment_name}/v{version}")
@asgi_router.websocket("/{deployment_name}/v{version}/{subpath:path}")
async def deployed_asgi_websocket_by_version(
    websocket: WebSocket,
    deployment_name: str,
    version: int,
    subpath: str = "",
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_websocket_services),
) -> None:
    workspace_id = authorize_websocket_workspace(services, websocket)
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Asgi,
        version=version,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    await _forward_asgi_websocket(
        stub,
        service,
        websocket,
        subpath=subpath,
    )


@asgi_router.api_route("/id/{stub_id}", methods=ASGI_METHODS, include_in_schema=False)
@asgi_router.api_route(
    "/id/{stub_id}/{subpath:path}",
    methods=ASGI_METHODS,
    include_in_schema=False,
)
async def deployed_asgi_request_by_id(
    stub_id: str,
    request: Request,
    subpath: str = "",
    *,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.Asgi,
        public=False,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    return await _forward_asgi_http_request(
        stub,
        service,
        request,
        subpath=subpath,
    )


@asgi_router.api_route("/public/{stub_id}", methods=ASGI_METHODS, include_in_schema=False)
@asgi_router.api_route(
    "/public/{stub_id}/{subpath:path}",
    methods=ASGI_METHODS,
    include_in_schema=False,
)
async def deployed_public_asgi_request_by_id(
    stub_id: str,
    request: Request,
    subpath: str = "",
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.Asgi,
        public=True,
        resource_name=ENDPOINT_RESOURCE_NAME,
    )
    return await _forward_asgi_http_request(
        stub,
        service,
        request,
        subpath=subpath,
    )


@asgi_router.api_route(
    "/{deployment_name}/latest",
    methods=ASGI_METHODS,
    include_in_schema=False,
)
@asgi_router.api_route(
    "/{deployment_name}/latest/{subpath:path}",
    methods=ASGI_METHODS,
    include_in_schema=False,
)
async def deployed_asgi_request_by_latest_path(
    deployment_name: str,
    request: Request,
    subpath: str = "",
    *,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Asgi,
        version=None,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    return await _forward_asgi_http_request(
        stub,
        service,
        request,
        subpath=subpath,
    )


@asgi_router.api_route(
    "/{deployment_name}/v{version}",
    methods=ASGI_METHODS,
    include_in_schema=False,
)
@asgi_router.api_route(
    "/{deployment_name}/v{version}/{subpath:path}",
    methods=ASGI_METHODS,
    include_in_schema=False,
)
async def deployed_asgi_request_by_version(
    deployment_name: str,
    version: int,
    request: Request,
    subpath: str = "",
    *,
    workspace_id: write_workspace,
    service: EndpointApiService = Depends(endpoint_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Asgi,
        version=version,
        resource_name=ENDPOINT_RESOURCE_NAME,
        workspace=workspace_id,
    )
    return await _forward_asgi_http_request(
        stub,
        service,
        request,
        subpath=subpath,
    )


async def _forwarded_request(
    stub: StubRecord, request: Request, subpath: str
) -> EndpointForwardRequest:
    return EndpointForwardRequest(
        stub_id=stub.id,
        method=request.method,
        path=forwarded_path(subpath),
        query_params=request_query_params(request),
        headers=request_headers(request),
        body=await request.body(),
    )


async def _health_probe_response(
    service: EndpointApiService,
    forwarded: EndpointForwardRequest,
) -> Response | None:
    """The probe path, which answers without opening an invocation.

    Both endpoint kinds share it, and neither can serve it the ordinary way: the
    ASGI path returns a stream it would have to metre, and the function path a
    task the caller never asked to run.
    """

    if forwarded.path != CONTAINER_HEALTH_PATH:
        return None
    result = await run_in_threadpool(service.forward_endpoint_health, forwarded)
    return forwarded_response(
        status_code=result.status_code, headers=result.headers, body=result.body
    )


async def _forward_endpoint_request(
    stub: StubRecord,
    service: EndpointApiService,
    request: Request,
    *,
    subpath: str = "",
) -> Response:
    forwarded = await _forwarded_request(stub, request, subpath)
    probe = await _health_probe_response(service, forwarded)
    if probe is not None:
        return probe
    result = await run_in_threadpool(service.forward_endpoint_request, forwarded)
    return forwarded_response(
        status_code=result.status_code, headers=result.headers, body=result.body
    )


async def _forward_asgi_http_request(
    stub: StubRecord,
    service: EndpointApiService,
    request: Request,
    *,
    subpath: str = "",
) -> Response:
    forwarded = await _forwarded_request(stub, request, subpath)
    probe = await _health_probe_response(service, forwarded)
    if probe is not None:
        return probe
    session: EndpointIngressDispatchSession | None = None
    try:
        session = await run_in_threadpool(service.prepare_asgi_http, forwarded)
        stream = await run_in_threadpool(service.open_asgi_http_stream, session, forwarded)
    except EndpointWebSocketDispatchRejected as exc:
        return Response(content=str(exc), status_code=exc.status_code)
    except Exception as exc:
        if session is not None:
            await run_in_threadpool(
                service.finish_asgi_http,
                session.task_id,
                error=str(exc),
            )
        return Response(content=str(exc), status_code=status.HTTP_502_BAD_GATEWAY)
    return _asgi_streaming_response(service, session, stream)


def _asgi_streaming_response(
    service: EndpointApiService,
    session: EndpointIngressDispatchSession,
    stream: EndpointResponseStream,
) -> StreamingResponse:
    response = StreamingResponse(
        _stream_asgi_response_body(service, session, stream),
        status_code=stream.status_code,
    )
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, values in stream.headers.items()
        if key.lower() not in HOP_BY_HOP_RESPONSE_HEADERS - {"content-length"}
        for value in values
    ]
    raw_headers.extend(
        [
            (b"x-task-id", session.task_id.encode("latin-1")),
            (b"access-control-expose-headers", b"X-Task-Id"),
        ]
    )
    response.raw_headers = raw_headers
    return response


def _stream_asgi_response_body(
    service: EndpointApiService,
    session: EndpointIngressDispatchSession,
    stream: EndpointResponseStream,
) -> Iterator[bytes]:
    body_size_bytes = 0
    completed = False
    error: str | None = None
    try:
        for chunk in stream.iter_chunks():
            body_size_bytes += len(chunk)
            yield chunk
        completed = True
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        stream.close()
        service.finish_asgi_http(
            session.task_id,
            status_code=stream.status_code,
            body_size_bytes=body_size_bytes,
            cancelled=not completed and error is None,
            error=error,
        )


def _start_deployed_endpoint_serve(
    stub: StubRecord,
    service: EndpointApiService,
) -> StartEndpointServeResponse:
    return service.start_endpoint_serve(StartEndpointServeRequest(stub_id=stub.id))


async def _forward_asgi_websocket(
    stub: StubRecord,
    service: EndpointApiService,
    websocket: WebSocket,
    *,
    subpath: str = "",
) -> None:
    forwarded = EndpointForwardRequest(
        stub_id=stub.id,
        method="GET",
        path=forwarded_path(subpath),
        query_params=websocket_query_params(websocket),
        headers=websocket_headers(websocket),
    )
    session: EndpointIngressDispatchSession | None = None
    backend: ClientConnection | None = None
    accepted = False
    cancelled = False
    error: str | None = None
    try:
        session = await run_in_threadpool(service.prepare_asgi_websocket, forwarded)
        backend = await _connect_backend_websocket(service, session, forwarded, websocket)
        await websocket.accept(subprotocol=backend.subprotocol)
        accepted = True
        heartbeat = asyncio.create_task(_heartbeat_asgi_websocket(service, session))
        try:
            cancelled = await _proxy_asgi_websocket(websocket, backend)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
    except EndpointWebSocketDispatchRejected as exc:
        await websocket.close(
            code=status.WS_1013_TRY_AGAIN_LATER,
            reason=str(exc)[:120],
        )
        error = str(exc)
    except WebSocketDisconnect:
        cancelled = True
    except Exception as exc:
        error = str(exc)
        if accepted:
            await close_websocket(websocket, code=status.WS_1011_INTERNAL_ERROR, reason=error)
        else:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason=error[:120])
    finally:
        if session is not None:
            await run_in_threadpool(
                service.finish_asgi_websocket,
                session.task_id,
                cancelled=cancelled,
                error=error,
            )
        if backend is not None:
            await backend.close()


async def _connect_backend_websocket(
    service: EndpointApiService,
    session: EndpointIngressDispatchSession,
    request: EndpointForwardRequest,
    websocket: WebSocket,
) -> ClientConnection:
    backend_socket = service.open_asgi_websocket_socket(session)
    subprotocols = websocket_subprotocols(websocket)
    return await websockets.asyncio.client.connect(
        endpoint_backend_url(
            session.target,
            request,
            protocol=EndpointBackendProtocol.WebSocket,
        ),
        additional_headers=backend_websocket_headers(session.headers),
        subprotocols=subprotocols or None,
        open_timeout=min(max(session.wait_timeout_seconds, 0.1), 10.0),
        proxy=None,
        sock=backend_socket,
    )


@dataclass(frozen=True, slots=True)
class BackendWebSocketClose:
    code: int
    reason: str


async def _heartbeat_asgi_websocket(
    service: EndpointApiService,
    session: EndpointIngressDispatchSession,
) -> None:
    interval = min(max(session.wait_timeout_seconds / 2, 0.1), 5.0)
    while True:
        await asyncio.sleep(interval)
        await run_in_threadpool(service.heartbeat_asgi_websocket, session.task_id)


async def _proxy_asgi_websocket(
    websocket: WebSocket,
    backend: ClientConnection,
) -> bool:
    backend_reader = asyncio.create_task(_backend_to_websocket(websocket, backend))
    client_reader = asyncio.create_task(_websocket_to_backend(websocket, backend))
    done, _pending = await asyncio.wait(
        {backend_reader, client_reader},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if backend_reader in done:
        close = await backend_reader
        await close_websocket(
            websocket,
            code=close.code,
            reason=close.reason,
        )
        client_reader.cancel()
        await asyncio.gather(client_reader, return_exceptions=True)
        return False
    cancelled = await client_reader
    backend_reader.cancel()
    await backend.close()
    await asyncio.gather(backend_reader, return_exceptions=True)
    return cancelled


async def _backend_to_websocket(
    websocket: WebSocket,
    backend: ClientConnection,
) -> BackendWebSocketClose:
    try:
        async for message in backend:
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(bytes(message))
    except ConnectionClosed:
        pass
    return BackendWebSocketClose(
        code=backend.close_code or status.WS_1011_INTERNAL_ERROR,
        reason=backend.close_reason or "backend websocket closed",
    )


async def _websocket_to_backend(
    websocket: WebSocket,
    backend: ClientConnection,
) -> bool:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return int(message.get("code") or status.WS_1000_NORMAL_CLOSURE) != (
                status.WS_1000_NORMAL_CLOSURE
            )
        data: Data | None = message.get("text")
        if data is None:
            data = message.get("bytes")
        if data is not None:
            await backend.send(data)


router.include_router(endpoint_router)
router.include_router(asgi_router)
