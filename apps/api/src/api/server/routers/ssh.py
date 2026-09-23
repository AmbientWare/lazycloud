from __future__ import annotations

from typing import Annotated

from execution.pods.proxy import PodProxyUnavailable
from execution.ssh.service import PodSshTunnelService, SshIdentityService
from fastapi import APIRouter, Depends, Query, WebSocket, status
from shared.errors import DomainError
from shared.http.ssh import SshCertificateRequest, SshCertificateResponse, SshHostKeyResponse
from shared.identity import AuthTokenRecord

from api.server.auth import read_workspace, write_token, write_workspace
from api.server.dependencies import (
    authorize_websocket_workspace,
    current_services,
    current_websocket_services,
)
from api.server.http import bridge_websocket_to_socket, close_websocket
from api.server.public_transfers import attribute_public_transfer
from api.server.services import ApiServices

SSH_TUNNEL_BUFFER_BYTES = 64 * 1024

router = APIRouter(tags=["ssh"])
AppQuery = Annotated[str, Query(min_length=1, description="Name of the app the pod belongs to.")]


def ssh_identity_service(
    services: Annotated[ApiServices, Depends(current_services)],
) -> SshIdentityService:
    return SshIdentityService(services.context.database)


def pod_ssh_tunnel_service(
    services: Annotated[ApiServices, Depends(current_websocket_services)],
) -> PodSshTunnelService:
    return PodSshTunnelService(
        async_database=services.require_async_io().database,
        pods=services.pod_service,
    )


@router.post(
    "/api/v1/ssh/certificates",
    response_model=SshCertificateResponse,
    operation_id="create_ssh_certificate",
)
def create_ssh_certificate(
    request: SshCertificateRequest,
    workspace_id: write_workspace,
    token: write_token,
    service: Annotated[SshIdentityService, Depends(ssh_identity_service)],
) -> SshCertificateResponse:
    return service.issue_user_certificate(
        workspace_id=workspace_id,
        holder=_certificate_holder(token),
        public_key=request.public_key,
    )


@router.get(
    "/api/v1/pods/{name}/ssh/host-key",
    response_model=SshHostKeyResponse,
    operation_id="get_pod_ssh_host_key",
)
def get_pod_ssh_host_key(
    name: str,
    app: AppQuery,
    workspace_id: read_workspace,
    service: Annotated[SshIdentityService, Depends(ssh_identity_service)],
) -> SshHostKeyResponse:
    return service.pod_host_key(workspace_id=workspace_id, app=app, pod=name)


@router.websocket("/api/v1/pods/{name}/ssh")
async def pod_ssh_tunnel(
    websocket: WebSocket,
    name: str,
    app: AppQuery,
    services: Annotated[ApiServices, Depends(current_websocket_services)],
    service: Annotated[PodSshTunnelService, Depends(pod_ssh_tunnel_service)],
) -> None:
    workspace_id = await authorize_websocket_workspace(services, websocket)
    await websocket.accept()
    try:
        async with service.open(workspace_id=workspace_id, app=app, pod=name) as tunnel:
            attribute_public_transfer(
                websocket,
                workspace_id=workspace_id,
                resource_type="stub",
                resource_id=tunnel.target.stub.id,
                stub_id=tunnel.target.stub.id,
            )
            await bridge_websocket_to_socket(websocket, tunnel.backend, SSH_TUNNEL_BUFFER_BYTES)
    except DomainError as exc:
        await close_websocket(websocket, code=status.WS_1008_POLICY_VIOLATION, reason=exc.message)
        return
    except PodProxyUnavailable as exc:
        await close_websocket(websocket, code=status.WS_1013_TRY_AGAIN_LATER, reason=str(exc))
        return
    except OSError as exc:
        await close_websocket(
            websocket,
            code=status.WS_1011_INTERNAL_ERROR,
            reason=f"pod SSH server is unreachable: {exc}",
        )
        return
    await close_websocket(websocket, code=status.WS_1000_NORMAL_CLOSURE, reason="")


def _certificate_holder(token: AuthTokenRecord) -> str:
    if token.user_id:
        return f"user:{token.user_id}"
    return f"token:{token.id}"
