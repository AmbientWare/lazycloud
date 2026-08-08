from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import AsyncIterator

from execution.shells.service import ShellControlService
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from fastapi.responses import StreamingResponse
from gateway.shell_proxy import connect_shell_backend
from identity.auth import AuthError
from identity.authz import AuthzRequirement
from identity.websocket_tickets import (
    ShellWebSocketAudience,
    ShellWebSocketAuthorization,
    WebSocketTicketService,
)
from networking.dialer import BackendRouteDialerConfig, BackendRouteResolver
from networking.tailnet import TailnetRuntime
from shared.errors import UpstreamUnavailableError
from shared.http.shells import (
    CreateShellInExistingContainerRequest,
    CreateShellInExistingContainerResponse,
    CreateStandaloneShellRequest,
    CreateStandaloneShellResponse,
    ShellConnectPlanResponse,
)
from shared.identity import AuthScope, AuthTokenRecord

from api.server.auth import read_token, read_workspace, write_workspace
from api.server.dependencies import (
    DEFAULT_WORKSPACE_NAME,
    authorize_token_workspace,
    current_services,
    current_websocket_services,
    websocket_authorization_header,
)
from api.server.service_dependencies import (
    backend_route_dialer_config,
    backend_route_resolver,
    shell_service,
    tailnet_runtime_service,
)
from api.server.services import ApiServices

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/shells", tags=["shell"])


@router.post("/standalone", response_model=CreateStandaloneShellResponse)
def create_standalone_shell(
    request: CreateStandaloneShellRequest,
    workspace_id: write_workspace,
    token: read_token,
    service: ShellControlService = Depends(shell_service),
    services: ApiServices = Depends(current_services),
) -> CreateStandaloneShellResponse:
    session = service.create_standalone_shell(
        workspace_id=workspace_id,
        stub_id=request.stub_id,
    )
    try:
        ticket = _mint_shell_ticket(
            services,
            token,
            workspace_id=workspace_id,
            stub_id=request.stub_id,
            container_id=session.container_id,
        )
    except Exception as exc:
        compensation = service.compensate_standalone_ticket_failure(
            workspace_id=workspace_id,
            container_id=session.container_id,
        )
        detail = "Shell session authorization is temporarily unavailable"
        if not compensation.succeeded:
            detail = f"{detail}; shell resource cleanup also failed"
        raise UpstreamUnavailableError(detail) from exc
    return CreateStandaloneShellResponse(
        container_id=session.container_id,
        username=session.username,
        password=session.password,
        websocket_ticket=ticket,
    )


@router.post(
    "/existing-container",
    response_model=CreateShellInExistingContainerResponse,
)
def create_shell_in_existing_container(
    request: CreateShellInExistingContainerRequest,
    workspace_id: write_workspace,
    token: read_token,
    service: ShellControlService = Depends(shell_service),
    services: ApiServices = Depends(current_services),
) -> CreateShellInExistingContainerResponse:
    session = service.create_shell_in_existing_container(
        workspace_id=workspace_id,
        container_id=request.container_id,
    )
    try:
        ticket = _mint_shell_ticket(
            services,
            token,
            workspace_id=workspace_id,
            stub_id=session.stub_id,
            container_id=request.container_id,
        )
    except Exception as exc:
        compensation = service.compensate_existing_container_ticket_failure(
            workspace_id=workspace_id,
            container_id=request.container_id,
        )
        detail = "Shell session authorization is temporarily unavailable"
        if not compensation.succeeded:
            detail = f"{detail}; shell resource cleanup also failed"
        raise UpstreamUnavailableError(detail) from exc
    return CreateShellInExistingContainerResponse(
        username=session.username,
        password=session.password,
        stub_id=session.stub_id,
        websocket_ticket=ticket,
    )


@router.get(
    "/connect-plan/{stub_id}/{container_id}",
    response_model=ShellConnectPlanResponse,
)
def shell_connect_plan(
    stub_id: str,
    container_id: str,
    workspace_id: read_workspace,
    service: ShellControlService = Depends(shell_service),
) -> ShellConnectPlanResponse:
    return service.connect_plan(
        stub_id=stub_id,
        container_id=container_id,
        workspace_id=workspace_id,
    )


@router.get("/id/{stub_id}/{container_id}", response_class=StreamingResponse)
async def shell_connect_tunnel(
    request: Request,
    stub_id: str,
    container_id: str,
    workspace_id: read_workspace,
    service: ShellControlService = Depends(shell_service),
    route_resolver: BackendRouteResolver = Depends(backend_route_resolver),
    route_dialer_config: BackendRouteDialerConfig = Depends(backend_route_dialer_config),
    tailnet_runtime: TailnetRuntime | None = Depends(tailnet_runtime_service),
) -> StreamingResponse:
    target = service.shell_backend_target(
        stub_id=stub_id,
        container_id=container_id,
        workspace_id=workspace_id,
    )
    try:
        backend = await asyncio.to_thread(
            connect_shell_backend,
            target,
            route_resolver=route_resolver,
            route_dialer_config=route_dialer_config,
            tailnet_peer_waiter=tailnet_runtime,
            tailnet_peer_resolver=tailnet_runtime,
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Failed to connect to container") from exc
    return StreamingResponse(
        _proxy_http_shell_stream(request, backend, target.buffer_size_bytes),
        media_type="application/octet-stream",
        headers={"cache-control": "no-store"},
    )


@router.websocket("/id/{stub_id}/{container_id}/ws")
async def shell_connect_websocket(
    websocket: WebSocket,
    stub_id: str,
    container_id: str,
    service: ShellControlService = Depends(shell_service),
    services: ApiServices = Depends(current_websocket_services),
    route_resolver: BackendRouteResolver = Depends(backend_route_resolver),
    route_dialer_config: BackendRouteDialerConfig = Depends(backend_route_dialer_config),
    tailnet_runtime: TailnetRuntime | None = Depends(tailnet_runtime_service),
) -> None:
    authorization = _authorize_shell_websocket(
        websocket,
        services,
        stub_id=stub_id,
        container_id=container_id,
    )
    await websocket.accept()
    target = None
    try:
        target = service.shell_backend_target(
            stub_id=stub_id,
            container_id=container_id,
            workspace_id=authorization.audience.workspace_id,
        )
        backend = await asyncio.to_thread(
            connect_shell_backend,
            target,
            route_resolver=route_resolver,
            route_dialer_config=route_dialer_config,
            tailnet_peer_waiter=tailnet_runtime,
            tailnet_peer_resolver=tailnet_runtime,
        )
    except Exception as exc:
        # Surface why the shell tunnel could not be established — without this
        # the client just sees an immediate disconnect and the cause (backend
        # dial failure, missing port publication) is invisible in the logs.
        backend_address = getattr(target, "address", "") if target is not None else ""
        logger.error(
            "shell tunnel failed for stub=%s container=%s backend=%s: %s",
            stub_id,
            container_id,
            backend_address or "<unresolved>",
            exc,
            exc_info=True,
        )
        await websocket.close(
            code=status.WS_1011_INTERNAL_ERROR,
            reason=str(exc)[:120],
        )
        return
    try:
        await websocket.send_text("OK")
        await _proxy_websocket_to_socket(websocket, backend, target.buffer_size_bytes)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    finally:
        backend.close()


async def _proxy_http_shell_stream(
    request: Request,
    backend: socket.socket,
    buffer_size_bytes: int,
) -> AsyncIterator[bytes]:
    backend.setblocking(False)
    loop = asyncio.get_running_loop()
    writer = asyncio.create_task(_request_body_to_socket(request, backend, loop))
    try:
        yield b"OK"
        while True:
            try:
                data = await loop.sock_recv(backend, max(buffer_size_bytes, 1))
            except OSError:
                return
            if not data:
                return
            yield data
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        backend.close()


async def _request_body_to_socket(
    request: Request,
    backend: socket.socket,
    loop: asyncio.AbstractEventLoop,
) -> None:
    try:
        async for chunk in request.stream():
            if chunk:
                await loop.sock_sendall(backend, chunk)
    except asyncio.CancelledError:
        raise
    except OSError:
        return


async def _proxy_websocket_to_socket(
    websocket: WebSocket,
    backend: socket.socket,
    buffer_size_bytes: int,
) -> None:
    backend.setblocking(False)
    loop = asyncio.get_running_loop()
    reader = asyncio.create_task(
        _socket_to_websocket(websocket, backend, max(buffer_size_bytes, 1))
    )
    writer = asyncio.create_task(_websocket_to_socket(websocket, backend, loop))
    done, pending = await asyncio.wait(
        {reader, writer},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)


async def _socket_to_websocket(
    websocket: WebSocket,
    backend: socket.socket,
    buffer_size_bytes: int,
) -> None:
    loop = asyncio.get_running_loop()
    while True:
        data = await loop.sock_recv(backend, buffer_size_bytes)
        if not data:
            return
        await websocket.send_bytes(data)


async def _websocket_to_socket(
    websocket: WebSocket,
    backend: socket.socket,
    loop: asyncio.AbstractEventLoop,
) -> None:
    while True:
        try:
            message = await websocket.receive()
        except WebSocketDisconnect:
            return
        if message["type"] == "websocket.disconnect":
            return
        data: bytes | None = message.get("bytes")
        if data is None:
            text: str | None = message.get("text")
            data = text.encode() if text is not None else b""
        if data:
            await loop.sock_sendall(backend, data)


def _token_workspace(services: ApiServices, token: AuthTokenRecord) -> str:
    """The workspace a websocket caller acts in.

    A workspace-scoped credential names its own. A person's names none, so this
    resolves the same default the HTTP dependency does and checks their membership
    before the socket is accepted.
    """
    if token.workspace_id:
        return token.workspace_id
    return authorize_token_workspace(
        services,
        token,
        DEFAULT_WORKSPACE_NAME,
        AuthScope.Read,
    )


def _mint_shell_ticket(
    services: ApiServices,
    token: AuthTokenRecord,
    *,
    workspace_id: str,
    stub_id: str,
    container_id: str,
) -> str:
    return WebSocketTicketService(services.context, services.redis_client).mint_shell_ticket(
        token,
        audience=ShellWebSocketAudience(
            workspace_id=workspace_id,
            stub_id=stub_id,
            container_id=container_id,
        ),
    )


def _authorize_shell_websocket(
    websocket: WebSocket,
    services: ApiServices,
    *,
    stub_id: str,
    container_id: str,
) -> ShellWebSocketAuthorization:
    try:
        ticket = websocket.query_params.get("ticket")
        if ticket is not None:
            if not ticket:
                raise AuthError("missing WebSocket ticket")
            return WebSocketTicketService(
                services.context,
                services.redis_client,
            ).consume_shell_ticket(
                ticket,
                stub_id=stub_id,
                container_id=container_id,
            )
        token = services.auth.authorize_header(
            websocket_authorization_header(websocket),
            AuthzRequirement(action=AuthScope.Read),
            allow_if_no_tokens=False,
        )
    except AuthError as exc:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=str(exc),
        ) from exc
    if token is None:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="missing authorization principal",
        )
    return ShellWebSocketAuthorization(
        token=token,
        audience=ShellWebSocketAudience(
            workspace_id=_token_workspace(services, token),
            stub_id=stub_id,
            container_id=container_id,
        ),
    )
