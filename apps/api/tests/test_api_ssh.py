from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from threading import Thread

import pytest
import uvicorn
from api.fastapi_app import create_app
from api.server.routers.ssh import pod_ssh_tunnel_service
from api.server.services import ApiServices
from control.service import ControlPlaneService
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from database.repositories.identity import WorkspaceRepository
from execution.ssh.keys import openssh_public_key, pod_host_key
from execution.ssh.service import PodSshTunnel, SshPodTarget, ssh_pod_target
from fastapi.testclient import TestClient
from identity.auth import TokenIssuer
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.ssh import SshCertificateResponse, SshHostKeyResponse
from starlette.websockets import WebSocketDisconnect
from tests.workspaces import owned_workspace, workspace_owner_user_id
from websockets.sync.client import connect


@dataclass(frozen=True, slots=True)
class _Fixture:
    workspace_id: str
    credential_secret: str
    app_id: str
    member: dict[str, str]
    outsider: dict[str, str]


def _user_headers(services: ApiServices, user_id: str) -> dict[str, str]:
    issuer = TokenIssuer(services.context)
    with services.context.database.session() as session:
        raw_token, _record = issuer.issue_for_user(session, "cli", user_id=user_id)
    issuer.committed()
    return {"Authorization": f"Bearer {raw_token}"}


def _credential_secret(services: ApiServices, workspace_id: str) -> str:
    with services.context.database.session() as session:
        secret = WorkspaceRepository(session).credential_secret(workspace_id)
    assert secret
    return secret


def _fixture(services: ApiServices) -> _Fixture:
    control = ControlPlaneService(services.context)
    workspace = owned_workspace(control, "ssh-owner")
    outsider_workspace = owned_workspace(control, "ssh-outsider")
    for name, ssh in (("box", True), ("web", False)):
        services.deployments.deploy(
            DeploymentSpec(name=name, kind=DeploymentKind.Pod, metadata={"app": "dev", "ssh": ssh}),
            workspace=workspace.id,
        )
    resources = services.deployment_resources.list(workspace=workspace.id, name="box")
    return _Fixture(
        workspace_id=workspace.id,
        credential_secret=_credential_secret(services, workspace.id),
        app_id=resources[0].app.id,
        member=_user_headers(services, workspace_owner_user_id(services.context, workspace.id)),
        outsider=_user_headers(
            services, workspace_owner_user_id(services.context, outsider_workspace.id)
        ),
    )


def test_ssh_certificate_and_host_key_are_served_only_to_members_for_ssh_pods(
    isolated_services: ApiServices,
) -> None:
    fixture = _fixture(isolated_services)
    public_key = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)
        .decode()
    )
    workspace = {"workspace": fixture.workspace_id}
    with ExitStack() as stack:
        client = stack.enter_context(TestClient(create_app(isolated_services)))

        certificate = client.post(
            "/api/v1/ssh/certificates",
            params=workspace,
            json={"public_key": public_key},
            headers=fixture.member,
        )
        refused_certificate = client.post(
            "/api/v1/ssh/certificates",
            params=workspace,
            json={"public_key": public_key},
            headers=fixture.outsider,
        )
        host_key = client.get(
            "/api/v1/pods/box/ssh/host-key",
            params={**workspace, "app": "dev"},
            headers=fixture.member,
        )
        refused_host_key = client.get(
            "/api/v1/pods/box/ssh/host-key",
            params={**workspace, "app": "dev"},
            headers=fixture.outsider,
        )
        not_ssh = client.get(
            "/api/v1/pods/web/ssh/host-key",
            params={**workspace, "app": "dev"},
            headers=fixture.member,
        )

    assert certificate.status_code == 200
    assert SshCertificateResponse.model_validate_json(certificate.content).certificate.startswith(
        "ssh-ed25519-cert-v01@openssh.com "
    )
    assert refused_certificate.status_code == 403
    assert host_key.status_code == 200
    assert SshHostKeyResponse.model_validate_json(
        host_key.content
    ).host_public_key == openssh_public_key(
        pod_host_key(fixture.credential_secret, app_id=fixture.app_id, pod_name="box"),
        comment="dev-box",
    )
    assert refused_host_key.status_code == 403
    assert not_ssh.status_code == 409


def test_ssh_tunnel_refuses_outsiders_and_pods_without_ssh(
    isolated_services: ApiServices,
) -> None:
    fixture = _fixture(isolated_services)
    query = f"workspace={fixture.workspace_id}&app=dev"
    with ExitStack() as stack:
        client = stack.enter_context(TestClient(create_app(isolated_services)))

        with (
            pytest.raises(WebSocketDisconnect) as outsider,
            client.websocket_connect(
                f"/api/v1/pods/box/ssh?{query}", headers=fixture.outsider
            ) as websocket,
        ):
            websocket.receive_bytes()
        with (
            client.websocket_connect(
                f"/api/v1/pods/web/ssh?{query}", headers=fixture.member
            ) as websocket,
            pytest.raises(WebSocketDisconnect) as not_ssh,
        ):
            websocket.receive_bytes()

    assert outsider.value.code == 1008
    assert not_ssh.value.code == 1008
    assert "does not serve SSH" in not_ssh.value.reason


@dataclass(frozen=True, slots=True)
class _WakingPod:
    target: SshPodTarget
    backend: socket.socket
    wake_seconds: float

    @asynccontextmanager
    async def open(self, *, workspace_id: str, app: str, pod: str) -> AsyncIterator[PodSshTunnel]:
        await asyncio.sleep(self.wake_seconds)
        yield PodSshTunnel(target=self.target, backend=self.backend)


def test_ssh_tunnel_accepts_at_once_and_holds_early_bytes_while_the_pod_wakes(
    isolated_services: ApiServices,
) -> None:
    fixture = _fixture(isolated_services)
    with isolated_services.context.database.session() as session:
        target = ssh_pod_target(session, workspace_id=fixture.workspace_id, app="dev", pod="box")
    tunnel_end, pod_end = socket.socketpair()
    pod_end.settimeout(5)
    ping_seconds = 0.1
    wake_seconds = 10 * ping_seconds
    app = create_app(isolated_services)
    app.dependency_overrides[pod_ssh_tunnel_service] = lambda: _WakingPod(
        target=target, backend=tunnel_end, wake_seconds=wake_seconds
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            ws_ping_interval=ping_seconds,
            ws_ping_timeout=ping_seconds,
        )
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    url = (
        f"ws://127.0.0.1:{listener.getsockname()[1]}/api/v1/pods/box/ssh"
        f"?workspace={fixture.workspace_id}&app=dev"
    )
    try:
        started = time.monotonic()
        with connect(
            url,
            additional_headers=fixture.member,
            open_timeout=wake_seconds,
            ping_interval=None,
        ) as websocket:
            accepted_after = time.monotonic() - started
            websocket.send(b"SSH-2.0-client\r\n")
            websocket.send(b"early-kexinit")
            early = b""
            while len(early) < len(b"SSH-2.0-client\r\nearly-kexinit") and (
                chunk := pod_end.recv(1024)
            ):
                early += chunk
            pod_end.sendall(b"SSH-2.0-pod\r\n")
            reply = websocket.recv(timeout=5)
        assert time.monotonic() - started >= wake_seconds
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        pod_end.close()
        tunnel_end.close()

    assert accepted_after < wake_seconds / 2
    assert early == b"SSH-2.0-client\r\nearly-kexinit"
    assert reply == b"SSH-2.0-pod\r\n"
