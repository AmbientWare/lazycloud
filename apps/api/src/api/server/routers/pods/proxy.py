from __future__ import annotations

import asyncio
from typing import Annotated
from urllib.parse import urlencode

import websockets.asyncio.client
from control.service import ControlPlaneService, StubKind, StubRecord
from execution.pods.planning import PodProxyProtocol
from execution.pods.proxy import (
    PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS,
    PodProxyBackendError,
    PodProxyHttpRequest,
    PodProxyHttpResponse,
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
    WebSocketException,
    status,
)
from identity.auth import AuthError
from identity.authz import AuthzRequirement
from shared.containers import ContainerRecord
from shared.errors import NotFoundError
from shared.identity import AuthScope, AuthTokenRecord
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed
from websockets.typing import Data, Subprotocol

from api.server.auth import write_app_token
from api.server.dependencies import (
    current_services,
    current_websocket_services,
    websocket_authorization_header,
)
from api.server.deployed_stubs import (
    resolve_deployed_stub,
    resolve_deployed_stub_id,
    token_workspace,
)
from api.server.http import (
    forwarded_path,
    request_headers,
    request_query_params,
    websocket_headers,
    websocket_query_params,
)
from api.server.service_dependencies import control_plane_service, pod_service
from api.server.services import ApiServices

POD_PROXY_METHODS = ["CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"]
HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
WEBSOCKET_BACKEND_HEADER_EXCLUDES = {
    "connection",
    "content-length",
    "host",
    "sec-websocket-accept",
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
    "upgrade",
}
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
    token: write_token,
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.Pod,
        public=False,
        workspace=token_workspace(token),
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
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
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
    token: write_token,
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Pod,
        version=None,
        workspace=token_workspace(token),
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
    token: write_token,
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Pod,
        version=version,
        workspace=token_workspace(token),
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
    token = _authorize_websocket(services, websocket)
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
        stub_id,
        StubKind.Pod,
        public=False,
        workspace=token_workspace(token),
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
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
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
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_websocket_services),
) -> None:
    token = _authorize_websocket(services, websocket)
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Pod,
        version=None,
        workspace=token_workspace(token),
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
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_websocket_services),
) -> None:
    token = _authorize_websocket(services, websocket)
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Pod,
        version=version,
        workspace=token_workspace(token),
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
    token: write_token,
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    container, stub = _resolve_sandbox_container(
        container_id,
        workspace=token_workspace(token),
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
    container, stub = _resolve_sandbox_container(
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
    token = _authorize_websocket(services, websocket)
    container, stub = _resolve_sandbox_container(
        container_id,
        workspace=token_workspace(token),
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
    container, stub = _resolve_sandbox_container(
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
    token: write_token,
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Sandbox,
        version=None,
        workspace=token_workspace(token),
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
    token: write_token,
    service: PodControlService = Depends(pod_service),
    control_plane: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> Response:
    stub = resolve_deployed_stub(
        control_plane,
        services,
        deployment_name,
        StubKind.Sandbox,
        version=version,
        workspace=token_workspace(token),
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
        result = await run_in_threadpool(service.forward_pod_http_request, proxy_request)
    except PodProxyPortUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PodProxyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PodProxyBackendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _http_response(result)


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
        session = await run_in_threadpool(
            service.prepare_pod_proxy,
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
            await _close_websocket(
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
            await run_in_threadpool(service.finish_pod_proxy, session)
        if backend is not None:
            await backend.close()


def _resolve_sandbox_container(
    container_id: str,
    *,
    workspace: str | None,
    public: bool,
    control_plane: ControlPlaneService,
    services: ApiServices,
) -> tuple[ContainerRecord, StubRecord]:
    try:
        container = services.containers.get(container_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="sandbox not found") from exc
    if container.stub_id is None or (workspace is not None and container.workspace_id != workspace):
        raise HTTPException(status_code=404, detail="sandbox not found")
    stub = resolve_deployed_stub_id(
        control_plane,
        services.apps,
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
    backend_socket = await run_in_threadpool(service.open_pod_proxy_socket, session)
    try:
        return await websockets.asyncio.client.connect(
            _pod_backend_websocket_url(request),
            additional_headers=_backend_websocket_headers(request.headers),
            subprotocols=_websocket_subprotocols(websocket) or None,
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
        await _close_websocket(websocket, code=code, reason=reason)
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


def _websocket_subprotocols(websocket: WebSocket) -> list[Subprotocol]:
    values: list[Subprotocol] = []
    for raw_value in websocket.headers.getlist("sec-websocket-protocol"):
        values.extend(Subprotocol(item.strip()) for item in raw_value.split(",") if item.strip())
    return values


def _backend_websocket_headers(headers: dict[str, list[str]]) -> list[tuple[str, str]]:
    forwarded: list[tuple[str, str]] = []
    for key, values in headers.items():
        if key.lower() in WEBSOCKET_BACKEND_HEADER_EXCLUDES:
            continue
        forwarded.extend((key, value) for value in values)
    return forwarded


async def _close_websocket(websocket: WebSocket, *, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason[:120])
    except RuntimeError:
        return


def _authorize_websocket(
    services: ApiServices,
    websocket: WebSocket,
) -> AuthTokenRecord:
    try:
        token = services.auth.authorize_header(
            websocket_authorization_header(websocket),
            AuthzRequirement(action=AuthScope.Write),
        )
        if token is None:
            raise AuthError("missing authorization principal")
        return token
    except AuthError as exc:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=str(exc),
        ) from exc


def _http_response(result: PodProxyHttpResponse) -> Response:
    response = Response(content=result.body, status_code=result.status_code)
    for key, values in result.headers.items():
        if key.lower() in HOP_BY_HOP_RESPONSE_HEADERS:
            continue
        for value in values:
            response.headers.append(key, value)
    return response


router.include_router(pod_router)
router.include_router(sandbox_router)
