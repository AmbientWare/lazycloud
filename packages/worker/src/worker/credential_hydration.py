from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from shared.container_requests import RequestMount, RequestMountType
from shared.identity import TokenKind
from shared.mounts import MountAuthMode

from worker.container_execution import ContainerExecutionContext
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.tools import (
    ContainerCredentialContext,
    ContainerCredentialRequest,
    ContainerCredentials,
    ContainerMount,
    ContainerMountKind,
    apply_container_credentials,
    build_container_credential_request,
    has_container_credential_request,
)


class ContainerCredentialVendor(Protocol):
    def vend(
        self,
        request: ContainerCredentialRequest,
        *,
        principal: WorkerCredentialPrincipal,
    ) -> ContainerCredentials: ...


@dataclass(slots=True)
class WorkerCredentialHydrator:
    credentials: ContainerCredentialVendor
    principal: WorkerCredentialPrincipal | None = None

    def hydrate_container_credentials(
        self,
        context: ContainerExecutionContext,
    ) -> ContainerExecutionContext:
        request = context.request
        principal = self._principal_for_request(request.workspace_id)
        container_mounts = [_container_mount_from_request_mount(mount) for mount in request.mounts]
        credential_request = build_container_credential_request(
            ContainerCredentialContext(
                workspace_id=request.workspace_id,
                stub_id=request.stub_id,
                container_id=request.container_id,
                secret_names=list(request.secret_names),
                gateway_token_required=request.gateway_token_required,
                workspace_storage_required=request.workspace_storage_required,
                mounts=container_mounts,
            )
        )

        hydrated_request = request
        changed = False
        if has_container_credential_request(credential_request):
            credentials = self.credentials.vend(credential_request, principal=principal)
            applied = apply_container_credentials(
                existing_env=request.env,
                mounts=container_mounts,
                credentials=credentials,
            )
            hydrated_request = hydrated_request.model_copy(
                update={
                    "env": applied.env,
                    "mounts": _apply_mount_credentials(request.mounts, applied.mounts),
                }
            )
            changed = True
            if applied.workspace_storage is not None:
                hydrated_request = hydrated_request.model_copy(
                    update={
                        "workspace_storage_available": True,
                        "workspace_storage_credentials": applied.workspace_storage,
                    }
                )

        if not changed:
            return context
        return context.model_copy(update={"request": hydrated_request})

    def _principal_for_request(self, workspace_id: str) -> WorkerCredentialPrincipal:
        if self.principal is not None:
            return self.principal
        return WorkerCredentialPrincipal(
            workspace_id=workspace_id,
            token_kind=TokenKind.Worker,
        )


def _container_mount_from_request_mount(mount: RequestMount) -> ContainerMount:
    config = mount.mountpoint_config
    kind = (
        ContainerMountKind.MountPoint
        if mount.mount_type is RequestMountType.MountPoint
        else ContainerMountKind.Local
    )
    return ContainerMount(
        mount_path=mount.mount_path,
        bucket_name=config.bucket_name if config is not None else "",
        kind=kind,
        auth_mode=config.auth_mode if config is not None else MountAuthMode.Ambient,
        access_key=config.access_key if config is not None else "",
        secret_key=config.secret_key if config is not None else "",
        endpoint_url=config.endpoint_url if config is not None else "",
        region=config.region if config is not None else "",
        force_path_style=config.force_path_style if config is not None else False,
    )


def _apply_mount_credentials(
    mounts: list[RequestMount],
    container_mounts: list[ContainerMount],
) -> list[RequestMount]:
    container_mount_by_key = {mount.credential_key: mount for mount in container_mounts}
    hydrated: list[RequestMount] = []
    for mount in mounts:
        config = mount.mountpoint_config
        if config is None:
            hydrated.append(mount)
            continue
        container_mount = container_mount_by_key.get(f"{mount.mount_path}:{config.bucket_name}")
        if container_mount is None:
            hydrated.append(mount)
            continue
        hydrated.append(
            mount.model_copy(
                update={
                    "mountpoint_config": config.model_copy(
                        update={
                            "access_key": container_mount.access_key,
                            "secret_key": container_mount.secret_key,
                            "endpoint_url": container_mount.endpoint_url,
                            "region": container_mount.region,
                            "force_path_style": container_mount.force_path_style,
                        }
                    )
                }
            )
        )
    return hydrated
