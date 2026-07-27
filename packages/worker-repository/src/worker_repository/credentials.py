"""Server-side worker credential vending services."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from control.service import ControlPlaneService
from database.types import DatabaseSession
from execution.mounts import volume_container_mount_paths
from identity.auth import AuthError, AuthService
from pydantic import field_validator
from shared.containers import ContainerRecord
from shared.contracts import ContractModel
from shared.env import GATEWAY_TOKEN_ENV
from shared.errors import NotFoundError
from shared.identity import TokenKind, WorkspaceRecord, WorkspaceStorageConfig
from shared.mounts import MountAuthMode
from shared.scheduling import SchedulerContainerState
from shared.secrets import SecretRecord
from shared.timestamps import utc_now
from shared.workload_config import (
    StubConfig,
    StubMountCredentialConfig,
    StubVolumeConfig,
)
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.tools import (
    ContainerCredentialRequest,
    ContainerCredentials,
    ContainerMountCredentials,
    WorkspaceStorageCredentials,
)

from database import DatabaseClient

DEFAULT_GATEWAY_TOKEN_TTL_SECONDS = 24 * 60 * 60


class WorkerCredentialError(PermissionError):
    pass


class WorkerCredentialContextPaths(Protocol):
    @property
    def root(self) -> Path: ...


class WorkerCredentialContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...

    @property
    def paths(self) -> WorkerCredentialContextPaths: ...

    def workspace(
        self,
        session: DatabaseSession,
        workspace: str = "default",
    ) -> WorkspaceRecord: ...

    def default_workspace_id(self, session: DatabaseSession) -> str: ...


class WorkerCredentialContainerLookup(Protocol):
    def get(self, container_id: str) -> ContainerRecord: ...


class WorkerCredentialContainerRepository(Protocol):
    def get_container_state(self, container_id: str) -> SchedulerContainerState | None: ...


class WorkerCredentialSecretLookup(Protocol):
    def get(self, name: str, *, workspace: str = "default") -> SecretRecord: ...


class WorkerCredentialServices(Protocol):
    @property
    def context(self) -> WorkerCredentialContext: ...

    @property
    def secrets(self) -> WorkerCredentialSecretLookup: ...


@runtime_checkable
class WorkerCredentialContainerServices(WorkerCredentialServices, Protocol):
    @property
    def containers(self) -> WorkerCredentialContainerLookup: ...


class ContainerMountCredentialSource(ContractModel):
    mount_path: str
    bucket_name: str
    access_key_secret: str
    secret_key_secret: str
    endpoint_url: str = ""
    region: str = ""
    force_path_style: bool = False

    @field_validator("mount_path", "bucket_name", "access_key_secret", "secret_key_secret")
    @classmethod
    def required_text(cls, value: str) -> str:
        if not value:
            msg = "mount credential source fields cannot be empty"
            raise ValueError(msg)
        return value

    @property
    def credential_key(self) -> str:
        return f"{self.mount_path}:{self.bucket_name}"


@dataclass(slots=True)
class _GatewayTokenLease:
    token: str
    expires_at: datetime


@dataclass(slots=True)
class WorkerCredentialService:
    services: WorkerCredentialServices | None = None
    container_repository: WorkerCredentialContainerRepository | None = None
    container_lookup: WorkerCredentialContainerLookup | None = None
    gateway_token_ttl_seconds: int = DEFAULT_GATEWAY_TOKEN_TTL_SECONDS
    _gateway_token_leases: dict[str, _GatewayTokenLease] = field(
        default_factory=dict, init=False, repr=False
    )
    _gateway_token_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def vend(
        self,
        request: ContainerCredentialRequest,
        *,
        principal: WorkerCredentialPrincipal,
    ) -> ContainerCredentials:
        self._authorize_request(request, principal=principal)
        self._validate_container_assignment(request)

        env: list[str] = []
        if request.secret_names:
            env.extend(self._secret_env(request.secret_names, workspace_id=request.workspace_id))
        if request.gateway_token:
            env.append(f"{GATEWAY_TOKEN_ENV}={self._gateway_token(request.workspace_id)}")

        workspace_storage = (
            self._workspace_storage_credentials(request.workspace_id)
            if request.workspace_storage
            else None
        )
        mount_credentials = self._mount_credentials(request) if request.mount_credentials else []
        return ContainerCredentials(
            env=env,
            workspace_storage=workspace_storage,
            mount_credentials=mount_credentials,
        )

    def _authorize_request(
        self,
        request: ContainerCredentialRequest,
        *,
        principal: WorkerCredentialPrincipal,
    ) -> None:
        if not principal.is_worker:
            msg = "worker token is required"
            raise WorkerCredentialError(msg)
        if principal.token_kind is TokenKind.WorkerPrivate and (
            not principal.workspace_id or principal.workspace_id != request.workspace_id
        ):
            msg = (
                "private worker token cannot request credentials for workspace "
                f"{request.workspace_id!r}"
            )
            raise WorkerCredentialError(msg)

    def _validate_container_assignment(self, request: ContainerCredentialRequest) -> None:
        if self.container_repository is not None:
            state = self.container_repository.get_container_state(request.container_id)
            if state is None:
                msg = f"container {request.container_id!r} is unavailable"
                raise WorkerCredentialError(msg)
            if state.workspace_id != request.workspace_id or state.stub_id != request.stub_id:
                msg = f"container {request.container_id!r} is not assigned to workspace/stub"
                raise WorkerCredentialError(msg)
            return

        container_lookup = self._container_lookup()
        if container_lookup is None:
            msg = f"container {request.container_id!r} is unavailable"
            raise WorkerCredentialError(msg)

        try:
            container = container_lookup.get(request.container_id)
        except NotFoundError as exc:
            msg = f"container {request.container_id!r} is unavailable"
            raise WorkerCredentialError(msg) from exc
        if container.workspace_id != request.workspace_id or container.stub_id != request.stub_id:
            msg = f"container {request.container_id!r} is not assigned to workspace/stub"
            raise WorkerCredentialError(msg)

    def _secret_env(self, secret_names: list[str], *, workspace_id: str) -> list[str]:
        env: list[str] = []
        seen: set[str] = set()
        for name in secret_names:
            if not name or name in seen:
                continue
            seen.add(name)
            try:
                value = self._secret_value(name, workspace_id=workspace_id)
            except NotFoundError as exc:
                msg = f"secret {name!r} is unavailable"
                raise WorkerCredentialError(msg) from exc
            env.append(f"{name}={value}")
        return env

    def _gateway_token(self, workspace_id: str) -> str:
        """Return a workspace-restricted gateway token for a container.

        Tokens are minted with a TTL and reused across container starts for the
        first half of that TTL, so repeated deploys/invocations do not flood the
        token table with one machine token per container.
        """
        reused = self._reusable_gateway_token(workspace_id)
        if reused is not None:
            return reused
        raw_token, record = AuthService(self._services().context).create_token(
            f"gateway-{workspace_id}",
            kind=TokenKind.WorkspaceRestricted,
            workspace_id=workspace_id,
            expires_in_seconds=self.gateway_token_ttl_seconds,
        )
        expires_at = record.expires_at or utc_now()
        with self._gateway_token_lock:
            self._gateway_token_leases[workspace_id] = _GatewayTokenLease(
                token=raw_token,
                expires_at=expires_at,
            )
        return raw_token

    def _reusable_gateway_token(self, workspace_id: str) -> str | None:
        with self._gateway_token_lock:
            lease = self._gateway_token_leases.get(workspace_id)
        if lease is None:
            return None
        remaining_seconds = (lease.expires_at - utc_now()).total_seconds()
        if remaining_seconds < self.gateway_token_ttl_seconds / 2:
            # Never hand a container a token in the back half of its lifetime.
            self._drop_gateway_token_lease(workspace_id, lease)
            return None
        try:
            AuthService(self._services().context).authenticate(lease.token)
        except AuthError:
            # Revoked, disabled, or deleted out of band; mint a fresh token.
            self._drop_gateway_token_lease(workspace_id, lease)
            return None
        return lease.token

    def _drop_gateway_token_lease(self, workspace_id: str, lease: _GatewayTokenLease) -> None:
        with self._gateway_token_lock:
            if self._gateway_token_leases.get(workspace_id) is lease:
                del self._gateway_token_leases[workspace_id]

    def _workspace_storage_credentials(self, workspace_id: str) -> WorkspaceStorageCredentials:
        workspace = ControlPlaneService(self._services().context).get_workspace(workspace_id)
        storage = workspace.storage
        if not storage.bucket:
            msg = f"workspace storage is unavailable for {workspace_id!r}"
            raise WorkerCredentialError(msg)
        return workspace_storage_credentials(storage)

    def _mount_credentials(
        self,
        request: ContainerCredentialRequest,
    ) -> list[ContainerMountCredentials]:
        stub = ControlPlaneService(self._services().context).get_stub(
            request.stub_id,
            workspace=request.workspace_id,
        )
        sources = mount_credential_sources(stub.config)
        source_by_key = {source.credential_key: source for source in sources}
        credentials: list[ContainerMountCredentials] = []
        seen: set[str] = set()
        for wanted in request.mount_credentials:
            if wanted.credential_key in seen:
                continue
            seen.add(wanted.credential_key)
            source = source_by_key.get(wanted.credential_key)
            if source is None:
                msg = f"mount credentials are unavailable for {wanted.mount_path}"
                raise WorkerCredentialError(msg)
            access_key = self._secret_value(
                source.access_key_secret,
                workspace_id=request.workspace_id,
            )
            secret_key = self._secret_value(
                source.secret_key_secret,
                workspace_id=request.workspace_id,
            )
            if not access_key or not secret_key:
                msg = f"mount credential secrets are empty for {wanted.mount_path}"
                raise WorkerCredentialError(msg)
            credentials.append(
                ContainerMountCredentials(
                    mount_path=wanted.mount_path,
                    bucket_name=source.bucket_name,
                    access_key=access_key,
                    secret_key=secret_key,
                    endpoint_url=source.endpoint_url,
                    region=source.region,
                    force_path_style=source.force_path_style,
                )
            )
        return credentials

    def _secret_value(self, name: str, *, workspace_id: str) -> str:
        return self._services().secrets.get(name, workspace=workspace_id).value

    def _services(self) -> WorkerCredentialServices:
        if self.services is None:
            msg = "worker credential service requires control-plane services"
            raise RuntimeError(msg)
        return self.services

    def _container_lookup(self) -> WorkerCredentialContainerLookup | None:
        if self.container_lookup is not None:
            return self.container_lookup
        services = self._services()
        if isinstance(services, WorkerCredentialContainerServices):
            return services.containers
        return None


def workspace_storage_credentials(storage: WorkspaceStorageConfig) -> WorkspaceStorageCredentials:
    return WorkspaceStorageCredentials(
        endpoint_url=storage.endpoint_url,
        region=storage.region,
        bucket_name=storage.bucket or "",
        access_key=storage.access_key,
        secret_key=storage.secret_key,
        force_path_style=storage.force_path_style,
    )


def mount_credential_sources(config: StubConfig) -> list[ContainerMountCredentialSource]:
    if config.mount_credentials:
        return _mount_credential_sources_from_mounts(config.mount_credentials)
    if config.mounts:
        return _mount_credential_sources_from_mounts(config.mounts)
    if config.volumes:
        return _mount_credential_sources_from_mounts(config.volumes)
    return []


def _mount_credential_sources_from_mounts(
    raw_mounts: list[StubMountCredentialConfig] | list[StubVolumeConfig],
) -> list[ContainerMountCredentialSource]:
    sources: list[ContainerMountCredentialSource] = []
    for raw_mount in raw_mounts:
        raw_config = raw_mount.credential_config
        if raw_config.auth_mode is not MountAuthMode.SecretReferences:
            continue
        mount_path = raw_mount.mount_path
        for source_mount_path in _credential_source_mount_paths(mount_path):
            try:
                sources.append(
                    ContainerMountCredentialSource(
                        mount_path=source_mount_path,
                        bucket_name=raw_config.bucket_name,
                        access_key_secret=raw_config.access_key,
                        secret_key_secret=raw_config.secret_key,
                        endpoint_url=raw_config.endpoint_url,
                        region=raw_config.region,
                        force_path_style=raw_config.force_path_style,
                    )
                )
            except ValueError:
                continue
    return sources


def _credential_source_mount_paths(mount_path: str) -> tuple[str, ...]:
    if not mount_path:
        return ("",)
    canonical, root = volume_container_mount_paths(mount_path)
    if root and root != canonical:
        return (canonical, root)
    return (canonical,)
