"""SSH access to deployed pods: certificates, host keys, container identity, tunnels."""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

from database.records.apps import StubRecord
from database.repositories.apps import (
    DeploymentResourceRepository,
    DeploymentResourceRow,
    StubRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.types import DatabaseSession
from shared.deployment_subdomains import deployment_subdomain
from shared.deployments import DeploymentKind, StubKind
from shared.errors import ConflictError, NotFoundError
from shared.http.ssh import SshCertificateResponse, SshHostKeyResponse
from shared.ssh import SSH_CERTIFICATE_PRINCIPAL, SSH_WORKER_PORT
from shared.timestamps import utc_now

from database import AsyncDatabaseClient, DatabaseClient
from execution.pods.planning import PodProxyProtocol
from execution.pods.service import PodControlService
from execution.ssh.keys import (
    openssh_private_key,
    openssh_public_key,
    pod_host_key,
    sign_user_certificate,
    workspace_user_authority,
)

USER_AUTHORITY_COMMENT = "lazycloud-user-ca"


@dataclass(frozen=True, slots=True)
class PodSshIdentity:
    """What a pod's SSH server needs: its own host key and the authority it trusts."""

    host_private_key: str = field(repr=False)
    user_ca_public_key: str


@dataclass(frozen=True, slots=True)
class SshPodTarget:
    workspace_id: str
    app_id: str
    app_name: str
    pod_name: str
    stub: StubRecord


@dataclass(frozen=True, slots=True)
class PodSshTunnel:
    target: SshPodTarget
    backend: socket.socket


@dataclass(slots=True)
class SshIdentityService:
    database: DatabaseClient
    clock: Callable[[], datetime] = utc_now

    def issue_user_certificate(
        self,
        *,
        workspace_id: str,
        holder: str,
        public_key: str,
    ) -> SshCertificateResponse:
        with self.database.session() as session:
            secret = _credential_secret(session, workspace_id)
        signed = sign_user_certificate(
            workspace_user_authority(secret),
            public_key=public_key,
            key_id=f"workspace={workspace_id} holder={holder}",
            now=self.clock(),
        )
        return SshCertificateResponse(
            certificate=signed.certificate,
            principal=SSH_CERTIFICATE_PRINCIPAL,
            expires_at=signed.expires_at,
        )

    def pod_host_key(self, *, workspace_id: str, app: str, pod: str) -> SshHostKeyResponse:
        with self.database.session() as session:
            target = ssh_pod_target(session, workspace_id=workspace_id, app=app, pod=pod)
            secret = _credential_secret(session, workspace_id)
        host_key = pod_host_key(secret, app_id=target.app_id, pod_name=target.pod_name)
        return SshHostKeyResponse(
            host_public_key=openssh_public_key(
                host_key, comment=f"{target.app_name}-{target.pod_name}"
            )
        )

    def container_identity(self, *, workspace_id: str, stub_id: str) -> PodSshIdentity:
        """The identity a container of this stub serves, refused unless it serves SSH."""
        with self.database.session() as session:
            row = _stub_deployment(session, workspace_id=workspace_id, stub_id=stub_id)
            secret = _credential_secret(session, workspace_id)
        host_key = pod_host_key(secret, app_id=row.app.id, pod_name=row.deployment.name)
        return PodSshIdentity(
            host_private_key=openssh_private_key(host_key),
            user_ca_public_key=openssh_public_key(
                workspace_user_authority(secret), comment=USER_AUTHORITY_COMMENT
            ),
        )


@dataclass(slots=True)
class PodSshTunnelService:
    async_database: AsyncDatabaseClient
    pods: PodControlService

    @asynccontextmanager
    async def open(self, *, workspace_id: str, app: str, pod: str) -> AsyncIterator[PodSshTunnel]:
        """Connect to the pod's SSH server and hold one proxy connection for the tunnel's life.

        The held connection is what wakes a scaled-to-zero pod and keeps it past its
        idle window while the tunnel is open.
        """
        target = await self.async_database.run_transaction(
            lambda session: ssh_pod_target(session, workspace_id=workspace_id, app=app, pod=pod)
        )
        session = await self.pods.prepare_pod_proxy(
            stub_id=target.stub.id,
            port=SSH_WORKER_PORT,
            path="",
            query_params={},
            protocol=PodProxyProtocol.Tcp,
        )
        try:
            backend = await self.pods.open_pod_proxy_socket(session)
            try:
                yield PodSshTunnel(target=target, backend=backend)
            finally:
                backend.close()
        finally:
            await self.pods.finish_pod_proxy(session)


def ssh_pod_target(
    session: DatabaseSession,
    *,
    workspace_id: str,
    app: str,
    pod: str,
) -> SshPodTarget:
    row = DeploymentResourceRepository(session).get_by_subdomain(
        deployment_subdomain(
            workspace_id=workspace_id,
            app_name=app,
            name=pod,
            kind=DeploymentKind.Pod,
        )
    )
    if (
        row is None
        or row.app.workspace_id != workspace_id
        or row.app.name != app
        or row.deployment.name != pod
        or row.deployment.kind is not DeploymentKind.Pod
    ):
        raise NotFoundError(f"pod not found: {app}/{pod}")
    if not row.deployment.active:
        raise ConflictError(f"pod {app}/{pod} is stopped; deploy it again to connect")
    _require_ssh(row.stub, f"{app}/{pod}")
    return SshPodTarget(
        workspace_id=workspace_id,
        app_id=row.app.id,
        app_name=row.app.name,
        pod_name=row.deployment.name,
        stub=row.stub,
    )


def _stub_deployment(
    session: DatabaseSession,
    *,
    workspace_id: str,
    stub_id: str,
) -> DeploymentResourceRow:
    stub = _stub(session, workspace_id=workspace_id, stub_id=stub_id)
    _require_ssh(stub, stub.name)
    if stub.deployment_id is None:
        raise ConflictError(f"pod {stub.name} serves SSH only once it is deployed")
    rows = DeploymentResourceRepository(session).list(
        workspace_id=workspace_id,
        app=None,
        app_id=None,
        deployment_id=stub.deployment_id,
        name=None,
        kinds=None,
        version=None,
        active=None,
    )
    if not rows:
        raise NotFoundError(f"deployment not found for stub {stub_id}")
    return rows[0]


def _stub(session: DatabaseSession, *, workspace_id: str, stub_id: str) -> StubRecord:
    stub = StubRepository(session).get(stub_id, workspace_id=workspace_id)
    if stub is None:
        raise NotFoundError(f"stub not found: {stub_id}")
    return stub


def _require_ssh(stub: StubRecord, label: str) -> None:
    if stub.kind is not StubKind.Pod or not stub.config.ssh:
        raise ConflictError(f"pod {label} does not serve SSH; deploy it with ssh=True")


def _credential_secret(session: DatabaseSession, workspace_id: str) -> str:
    secret = WorkspaceRepository(session).credential_secret(workspace_id)
    if secret is None:
        raise NotFoundError(f"workspace not found: {workspace_id}")
    return secret


__all__ = [
    "PodSshIdentity",
    "PodSshTunnel",
    "PodSshTunnelService",
    "SshIdentityService",
    "SshPodTarget",
    "ssh_pod_target",
]
