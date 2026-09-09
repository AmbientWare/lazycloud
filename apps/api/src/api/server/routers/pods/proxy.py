from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated
from urllib.parse import urlencode

import websockets.asyncio.client
from control.service import ControlPlaneService, StubKind, StubRecord
from execution.pods.planning import PodProxyProtocol
from execution.pods.proxy import (
    PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS,
    PodProxyBackendError,
    PodProxyHttpRequest,
    PodProxyPortUnavailable,
    PodProxySession,
    PodProxyUnavailable,
)
from execution.pods.service import PodControlService
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Request,
    Response,
    WebSocket,
    status,
)
from shared.containers import ContainerRecord
from shared.errors import NotFoundError
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed
from websockets.typing import Data

from api.server.auth import write_app_token, write_workspace
from api.server.dependencies import (
    authorize_websocket_workspace,
    current_services,
    current_websocket_services,
)
from api.server.deployed_stubs import (
    resolve_deployed_stub_async,
    resolve_deployed_stub_id_async,
)
from api.server.http import (
    backend_websocket_headers,
    close_websocket,
    forwarded_path,
    forwarded_streaming_response,
    request_headers,
    request_query_params,
    websocket_headers,
    websocket_query_params,
    websocket_subprotocols,
)
from api.server.public_transfers import attribute_public_transfer
from api.server.service_dependencies import control_plane_service, pod_service
from api.server.services import ApiServices

POD_PROXY_METHODS = ["CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"]
PortPath = Annotated[int, Path(ge=1, le=65535)]
VersionPath = Annotated[int, Path(ge=1)]

router = APIRouter()
pod_router = APIRouter(prefix="/pod", tags=["pod"])
sandbox_router = APIRouter(prefix="/sandbox", tags=["sandbox"])
write_token = write_app_token


@pod_router.api_route(
    "/id/{stub_id}/{port}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
@pod_router.api_route(
    "/id/{stub_id}/{port}/{subpath:path}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
async def deployed_pod_proxy_by_id(
    stub_id: str,
    port: PortPath,
    request: Request,
    subpath: str = "",
    *,
    workspace_id: write_workspace,
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = await resolve_deployed_stub_id_async(
        control_plane,
        services,
        stub_id,
        StubKind.Pod,
        public=False,
        workspace=workspace_id,
    )
    return await _forward_proxy_request(stub, port, request, subpath, service)


@pod_router.api_route(
    "/public/{stub_id}/{port}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
@pod_router.api_route(
    "/public/{stub_id}/{port}/{subpath:path}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
async def deployed_public_pod_proxy_by_id(
    stub_id: str,
    port: PortPath,
    request: Request,
    subpath: str = "",
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = await resolve_deployed_stub_id_async(
        control_plane,
        services,
        stub_id,
        StubKind.Pod,
        public=True,
    )
    return await _forward_proxy_request(stub, port, request, subpath, service)


@pod_router.api_route(
    "/{deployment_name}/latest/{port}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
@pod_router.api_route(
    "/{deployment_name}/latest/{port}/{subpath:path}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
async def deployed_pod_proxy_by_latest_path(
    deployment_name: str,
    port: PortPath,
    request: Request,
    subpath: str = "",
    *,
    workspace_id: write_workspace,
    service: PodControlService = Depends(pod_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = await resolve_deployed_stub_async(
        services,
        deployment_name,
        StubKind.Pod,
        version=None,
        workspace=workspace_id,
    )
    return await _forward_proxy_request(stub, port, request, subpath, service)


@pod_router.api_route(
    "/{deployment_name}/v{version}/{port}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
@pod_router.api_route(
    "/{deployment_name}/v{version}/{port}/{subpath:path}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
async def deployed_pod_proxy_by_version(
    deployment_name: str,
    version: VersionPath,
    port: PortPath,
    request: Request,
    subpath: str = "",
    *,
    workspace_id: write_workspace,
    service: PodControlService = Depends(pod_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = await resolve_deployed_stub_async(
        services,
        deployment_name,
        StubKind.Pod,
        version=version,
        workspace=workspace_id,
    )
    return await _forward_proxy_request(stub, port, request, subpath, service)


@pod_router.websocket("/id/{stub_id}/{port}")
@pod_router.websocket("/id/{stub_id}/{port}/{subpath:path}")
async def deployed_pod_websocket_by_id(
    websocket: WebSocket,
    stub_id: str,
    port: PortPath,
    subpath: str = "",
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_websocket_services),
) -> None:
    workspace_id = await authorize_websocket_workspace(services, websocket)
    stub = await resolve_deployed_stub_id_async(
        control_plane,
        services,
        stub_id,
        StubKind.Pod,
        public=False,
        workspace=workspace_id,
    )
    await _forward_pod_websocket(stub, port, websocket, subpath, service)


@pod_router.websocket("/public/{stub_id}/{port}")
@pod_router.websocket("/public/{stub_id}/{port}/{subpath:path}")
async def deployed_public_pod_websocket_by_id(
    websocket: WebSocket,
    stub_id: str,
    port: PortPath,
    subpath: str = "",
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_websocket_services),
) -> None:
    stub = await resolve_deployed_stub_id_async(
        control_plane,
        services,
        stub_id,
        StubKind.Pod,
        public=True,
    )
    await _forward_pod_websocket(stub, port, websocket, subpath, service)


@pod_router.websocket("/{deployment_name}/latest/{port}")
@pod_router.websocket("/{deployment_name}/latest/{port}/{subpath:path}")
async def deployed_pod_websocket_by_latest_path(
    websocket: WebSocket,
    deployment_name: str,
    port: PortPath,
    subpath: str = "",
    service: PodControlService = Depends(pod_service),
    services: ApiServices = Depends(current_websocket_services),
) -> None:
    workspace_id = await authorize_websocket_workspace(services, websocket)
    stub = await resolve_deployed_stub_async(
        services,
        deployment_name,
        StubKind.Pod,
        version=None,
        workspace=workspace_id,
    )
    await _forward_pod_websocket(stub, port, websocket, subpath, service)


@pod_router.websocket("/{deployment_name}/v{version}/{port}")
@pod_router.websocket("/{deployment_name}/v{version}/{port}/{subpath:path}")
async def deployed_pod_websocket_by_version(
    websocket: WebSocket,
    deployment_name: str,
    version: VersionPath,
    port: PortPath,
    subpath: str = "",
    service: PodControlService = Depends(pod_service),
    services: ApiServices = Depends(current_websocket_services),
) -> None:
    workspace_id = await authorize_websocket_workspace(services, websocket)
    stub = await resolve_deployed_stub_async(
        services,
        deployment_name,
        StubKind.Pod,
        version=version,
        workspace=workspace_id,
    )
    await _forward_pod_websocket(stub, port, websocket, subpath, service)


@sandbox_router.api_route(
    "/id/{container_id}/{port}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
@sandbox_router.api_route(
    "/id/{container_id}/{port}/{subpath:path}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
async def deployed_sandbox_proxy_by_id(
    container_id: str,
    port: PortPath,
    request: Request,
    subpath: str = "",
    *,
    workspace_id: write_workspace,
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    container, stub = await _resolve_sandbox_container(
        container_id,
        workspace=workspace_id,
        public=False,
        control_plane=control_plane,
        services=services,
    )
    return await _forward_proxy_request(
        stub,
        port,
        request,
        subpath,
        service,
        container_id=container.id,
    )


@sandbox_router.api_route(
    "/public/{container_id}/{port}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
@sandbox_router.api_route(
    "/public/{container_id}/{port}/{subpath:path}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
async def deployed_public_sandbox_proxy_by_id(
    container_id: str,
    port: PortPath,
    request: Request,
    subpath: str = "",
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    container, stub = await _resolve_sandbox_container(
        container_id,
        workspace=None,
        public=True,
        control_plane=control_plane,
        services=services,
    )
    return await _forward_proxy_request(
        stub,
        port,
        request,
        subpath,
        service,
        container_id=container.id,
    )


@sandbox_router.websocket("/id/{container_id}/{port}")
@sandbox_router.websocket("/id/{container_id}/{port}/{subpath:path}")
async def deployed_sandbox_websocket_by_id(
    websocket: WebSocket,
    container_id: str,
    port: PortPath,
    subpath: str = "",
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_websocket_services),
) -> None:
    workspace_id = await authorize_websocket_workspace(services, websocket)
    container, stub = await _resolve_sandbox_container(
        container_id,
        workspace=workspace_id,
        public=False,
        control_plane=control_plane,
        services=services,
    )
    await _forward_pod_websocket(
        stub,
        port,
        websocket,
        subpath,
        service,
        container_id=container.id,
    )


@sandbox_router.websocket("/public/{container_id}/{port}")
@sandbox_router.websocket("/public/{container_id}/{port}/{subpath:path}")
async def deployed_public_sandbox_websocket_by_id(
    websocket: WebSocket,
    container_id: str,
    port: PortPath,
    subpath: str = "",
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_websocket_services),
) -> None:
    container, stub = await _resolve_sandbox_container(
        container_id,
        workspace=None,
        public=True,
        control_plane=control_plane,
        services=services,
    )
    await _forward_pod_websocket(
        stub,
        port,
        websocket,
        subpath,
        service,
        container_id=container.id,
    )


@sandbox_router.api_route(
    "/{deployment_name}/latest/{port}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
@sandbox_router.api_route(
    "/{deployment_name}/latest/{port}/{subpath:path}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
async def deployed_sandbox_proxy_by_latest_path(
    deployment_name: str,
    port: PortPath,
    request: Request,
    subpath: str = "",
    *,
    workspace_id: write_workspace,
    service: PodControlService = Depends(pod_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = await resolve_deployed_stub_async(
        services,
        deployment_name,
        StubKind.Sandbox,
        version=None,
        workspace=workspace_id,
    )
    return await _forward_proxy_request(stub, port, request, subpath, service)


@sandbox_router.api_route(
    "/{deployment_name}/v{version}/{port}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
@sandbox_router.api_route(
    "/{deployment_name}/v{version}/{port}/{subpath:path}",
    methods=POD_PROXY_METHODS,
    include_in_schema=False,
)
async def deployed_sandbox_proxy_by_version(
    deployment_name: str,
    version: VersionPath,
    port: PortPath,
    request: Request,
    subpath: str = "",
    *,
    workspace_id: write_workspace,
    service: PodControlService = Depends(pod_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = await resolve_deployed_stub_async(
        services,
        deployment_name,
        StubKind.Sandbox,
        version=version,
        workspace=workspace_id,
    )
    return await _forward_proxy_request(stub, port, request, subpath, service)


async def _forward_proxy_request(
    stub: StubRecord,
    port: int,
    request: Request,
    subpath: str,
    service: PodControlService,
    *,
    container_id: str | None = None,
) -> Response:
    proxy_request = PodProxyHttpRequest(
        stub_id=stub.id,
        container_id=container_id,
        port=port,
        method=request.method,
        path=forwarded_path(subpath),
        query_params=request_query_params(request),
        headers=request_headers(request),
        body=await request.body(),
    )
    try:
        session = await service.prepare_pod_proxy(
            stub_id=stub.id,
            container_id=container_id,
            port=port,
            path=proxy_request.path,
            query_params=proxy_request.query_params,
            protocol=PodProxyProtocol.Http,
        )
        try:
            result = await service.open_pod_proxy_http_stream(session, proxy_request)
        except BaseException:
            await service.finish_pod_proxy(session)
            raise
    except PodProxyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PodProxyBackendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in result.iter_chunks():
                yield chunk
        finally:
            await result.close()
            await service.finish_pod_proxy(session)

    attribute_public_transfer(
        request,
        workspace_id=stub.workspace_id,
        resource_type="stub",
        resource_id=stub.id,
        stub_id=stub.id,
    )
    return forwarded_streaming_response(
        status_code=result.status_code,
        headers=result.headers,
        body=body(),
    )


async def _forward_pod_websocket(
    stub: StubRecord,
    port: int,
    websocket: WebSocket,
    subpath: str,
    service: PodControlService,
    *,
    container_id: str | None = None,
) -> None:
    request = PodProxyHttpRequest(
        stub_id=stub.id,
        container_id=container_id,
        port=port,
        method="GET",
        path=forwarded_path(subpath),
        query_params=websocket_query_params(websocket),
        headers=websocket_headers(websocket),
    )
    session: PodProxySession | None = None
    backend: ClientConnection | None = None
    accepted = False
    try:
        session = await service.prepare_pod_proxy(
            stub_id=stub.id,
            container_id=container_id,
            port=port,
            path=request.path,
            query_params=request.query_params,
            protocol=PodProxyProtocol.WebSocket,
        )
        backend = await _connect_backend_websocket(service, session, request, websocket)
        await websocket.accept(subprotocol=backend.subprotocol)
        accepted = True
        attribute_public_transfer(
            websocket,
            workspace_id=stub.workspace_id,
            resource_type="stub",
            resource_id=stub.id,
            stub_id=stub.id,
        )
        await _proxy_pod_websocket(websocket, backend)
    except (PodProxyPortUnavailable, PodProxyUnavailable) as exc:
        if session is not None and not accepted:
            await websocket.accept()
            accepted = True
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER, reason=str(exc)[:120])
    except WebSocketDisconnect:
        return
    except Exception as exc:
        if accepted:
            await close_websocket(
                websocket,
                code=status.WS_1011_INTERNAL_ERROR,
                reason=str(exc),
            )
        else:
            await websocket.close(
                code=status.WS_1011_INTERNAL_ERROR,
                reason=str(exc)[:120],
            )
    finally:
        if session is not None:
            await service.finish_pod_proxy(session)
        if backend is not None:
            await backend.close()


async def _resolve_sandbox_container(
    container_id: str,
    *,
    workspace: str | None,
    public: bool,
    control_plane: ControlPlaneService,
    services: ApiServices,
) -> tuple[ContainerRecord, StubRecord]:
    try:
        container = await services.require_async_io().database.run_transaction(
            lambda session: services.containers.get_in_session(session, container_id)
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="sandbox not found") from exc
    if container.stub_id is None or (workspace is not None and container.workspace_id != workspace):
        raise HTTPException(status_code=404, detail="sandbox not found")
    stub = await resolve_deployed_stub_id_async(
        control_plane,
        services,
        container.stub_id,
        StubKind.Sandbox,
        public=public,
        resource_name="sandbox",
        workspace=workspace,
    )
    if container.stub_id != stub.id or container.workspace_id != stub.workspace_id:
        raise HTTPException(status_code=404, detail="sandbox not found")
    return container, stub


async def _connect_backend_websocket(
    service: PodControlService,
    session: PodProxySession,
    request: PodProxyHttpRequest,
    websocket: WebSocket,
) -> ClientConnection:
    backend_socket = await service.open_pod_proxy_socket(session)
    try:
        return await websockets.asyncio.client.connect(
            _pod_backend_websocket_url(request),
            additional_headers=backend_websocket_headers(request.headers),
            subprotocols=websocket_subprotocols(websocket) or None,
            open_timeout=(
                PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS
                if session.pinned
                else min(max(service.pod_proxy_start_timeout_seconds, 0.1), 10.0)
            ),
            proxy=None,
            sock=backend_socket,
        )
    except Exception as exc:
        backend_socket.close()
        if session.pinned:
            raise PodProxyUnavailable("sandbox backend is unavailable") from exc
        raise


async def _proxy_pod_websocket(
    websocket: WebSocket,
    backend: ClientConnection,
) -> None:
    backend_reader = asyncio.create_task(_backend_to_websocket(websocket, backend))
    client_reader = asyncio.create_task(_websocket_to_backend(websocket, backend))
    done, _pending = await asyncio.wait(
        {backend_reader, client_reader},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if backend_reader in done:
        code, reason = await backend_reader
        await close_websocket(websocket, code=code, reason=reason)
        client_reader.cancel()
        await asyncio.gather(client_reader, return_exceptions=True)
        return
    await client_reader
    backend_reader.cancel()
    await asyncio.gather(backend_reader, return_exceptions=True)


async def _backend_to_websocket(
    websocket: WebSocket,
    backend: ClientConnection,
) -> tuple[int, str]:
    try:
        async for message in backend:
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(bytes(message))
    except ConnectionClosed:
        pass
    return (
        backend.close_code or status.WS_1011_INTERNAL_ERROR,
        backend.close_reason or "backend websocket closed",
    )


async def _websocket_to_backend(
    websocket: WebSocket,
    backend: ClientConnection,
) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data: Data | None = message.get("text")
        if data is None:
            data = message.get("bytes")
        if data is not None:
            await backend.send(data)


def _pod_backend_websocket_url(request: PodProxyHttpRequest) -> str:
    query = urlencode(
        [(key, value) for key, values in request.query_params.items() for value in values]
    )
    suffix = f"?{query}" if query else ""
    return f"ws://pod{request.path}{suffix}"


router.include_router(pod_router)
router.include_router(sandbox_router)
