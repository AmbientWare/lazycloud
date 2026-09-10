from __future__ import annotations

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from identity.auth import AuthService
from shared.container_requests import RequestMount, RequestMountPointConfig, RequestMountType
from shared.identity import TokenKind, WorkspaceStorageConfig
from shared.mounts import MountAuthMode
from storage.workspace_storage_issuers import StoredWorkspaceStorageIssuer
from tests.workspaces import owned_workspace
from worker.container_execution import ContainerExecutionContext
from worker.credential_hydration import WorkerCredentialHydrator
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.events import ContainerRequestContext
from worker.tools import ContainerCredentialRequest, ContainerMountCredentialRequest
from worker_repository.credentials import (
    GATEWAY_TOKEN_ENV,
    WorkerCredentialError,
    WorkerCredentialService,
)


def _credential_service(services: ApiServices) -> WorkerCredentialService:
    """The service as production composes it, which is never without an issuer."""
    return WorkerCredentialService(services, storage_issuer=StoredWorkspaceStorageIssuer())


def test_worker_credential_service_vends_requested_bundle(isolated_services: ApiServices) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(
        control,
        "default",
        storage=WorkspaceStorageConfig(
            backend="s3",
            bucket="workspace-bucket",
            prefix="workspace-a",
            config={
                "endpoint_url": "https://s3.local",
                "region": "us-test-1",
                "access_key": "workspace-ak",
                "secret_key": "workspace-sk",
                "force_path_style": True,
            },
        ),
    )
    isolated_services.secrets.set("API_TOKEN", "secret-value")
    isolated_services.secrets.set("MOUNT_ACCESS_KEY", "mount-ak")
    isolated_services.secrets.set("MOUNT_SECRET_KEY", "mount-sk")
    stub = control.create_stub(
        "worker",
        workspace=workspace.id,
        config={
            "mount_credentials": [
                {
                    "mount_path": "/mnt/data",
                    "bucket_name": "mount-bucket",
                    "auth_mode": "secret_references",
                    "access_key": "MOUNT_ACCESS_KEY",
                    "secret_key": "MOUNT_SECRET_KEY",
                    "endpoint_url": "https://mount.local",
                    "region": "us-test-2",
                }
            ]
        },
    )
    container = isolated_services.containers.run(
        "worker-container",
        image="python:3.12",
        command="true",
        workspace_id=workspace.id,
        stub_id=stub.id,
    )

    service = WorkerCredentialService(
        isolated_services, storage_issuer=StoredWorkspaceStorageIssuer()
    )
    credentials = service.vend(
        ContainerCredentialRequest(
            workspace_id=workspace.id,
            stub_id=stub.id,
            container_id=container.id,
            secret_names=["API_TOKEN", "API_TOKEN", ""],
            gateway_token=True,
            workspace_storage=True,
            mount_credentials=[
                ContainerMountCredentialRequest(
                    mount_path="/mnt/data",
                    bucket_name="mount-bucket",
                )
            ],
        ),
        principal=WorkerCredentialPrincipal(
            workspace_id=workspace.id,
            token_kind=TokenKind.Worker,
        ),
    )

    assert credentials.env[0] == "API_TOKEN=secret-value"
    gateway_token_entries = [
        item for item in credentials.env if item.startswith(f"{GATEWAY_TOKEN_ENV}=")
    ]
    assert len(gateway_token_entries) == 1
    assert gateway_token_entries[0].split("=", 1)[1].startswith("rt_")
    assert len([item for item in credentials.env if item.startswith("API_TOKEN=")]) == 1
    assert credentials.workspace_storage is not None
    assert credentials.workspace_storage.bucket_name == "workspace-bucket"
    assert credentials.workspace_storage.force_path_style
    assert credentials.mount_credentials[0].access_key == "mount-ak"
    assert credentials.mount_credentials[0].secret_key == "mount-sk"

    restricted_tokens = [
        token
        for token in AuthService(isolated_services.context).list_tokens()
        if token.kind is TokenKind.WorkspaceRestricted and token.workspace_id == workspace.id
    ]
    assert restricted_tokens
    assert restricted_tokens[0].expires_at is not None


def test_worker_credential_service_reuses_gateway_token_across_containers(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "default")
    stub = control.create_stub("worker", workspace=workspace.id)
    service = WorkerCredentialService(
        isolated_services, storage_issuer=StoredWorkspaceStorageIssuer()
    )
    principal = WorkerCredentialPrincipal(
        workspace_id=workspace.id,
        token_kind=TokenKind.Worker,
    )

    tokens: list[str] = []
    for index in range(3):
        container = isolated_services.containers.run(
            f"worker-container-{index}",
            image="python:3.12",
            command="true",
            workspace_id=workspace.id,
            stub_id=stub.id,
        )
        credentials = service.vend(
            ContainerCredentialRequest(
                workspace_id=workspace.id,
                stub_id=stub.id,
                container_id=container.id,
                gateway_token=True,
            ),
            principal=principal,
        )
        entry = next(item for item in credentials.env if item.startswith(f"{GATEWAY_TOKEN_ENV}="))
        tokens.append(entry.split("=", 1)[1])

    assert len(set(tokens)) == 1
    restricted_tokens = [
        token
        for token in AuthService(isolated_services.context).list_tokens()
        if token.kind is TokenKind.WorkspaceRestricted and token.workspace_id == workspace.id
    ]
    assert len(restricted_tokens) == 1


def test_worker_credential_service_replaces_revoked_or_aging_gateway_tokens(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "default")
    stub = control.create_stub("worker", workspace=workspace.id)
    service = WorkerCredentialService(
        isolated_services, storage_issuer=StoredWorkspaceStorageIssuer()
    )
    principal = WorkerCredentialPrincipal(
        workspace_id=workspace.id,
        token_kind=TokenKind.Worker,
    )

    def vend_gateway_token(container_name: str) -> str:
        container = isolated_services.containers.run(
            container_name,
            image="python:3.12",
            command="true",
            workspace_id=workspace.id,
            stub_id=stub.id,
        )
        credentials = service.vend(
            ContainerCredentialRequest(
                workspace_id=workspace.id,
                stub_id=stub.id,
                container_id=container.id,
                gateway_token=True,
            ),
            principal=principal,
        )
        entry = next(item for item in credentials.env if item.startswith(f"{GATEWAY_TOKEN_ENV}="))
        return entry.split("=", 1)[1]

    first = vend_gateway_token("worker-container-a")

    auth = AuthService(isolated_services.context)
    restricted = [
        token
        for token in auth.list_tokens()
        if token.kind is TokenKind.WorkspaceRestricted and token.workspace_id == workspace.id
    ]
    auth.revoke_token(restricted[0].id)
    after_revoke = vend_gateway_token("worker-container-b")
    assert after_revoke != first

    # A lease in the back half of its lifetime is replaced instead of reused:
    # widening the TTL leaves the outstanding lease with less than half of it.
    service.gateway_token_ttl_seconds *= 4
    after_aging = vend_gateway_token("worker-container-c")
    assert after_aging != after_revoke


def test_worker_credential_service_resolves_volume_secret_names(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "default")
    isolated_services.secrets.set("MOUNT_ACCESS_KEY", "mount-ak")
    isolated_services.secrets.set("MOUNT_SECRET_KEY", "mount-sk")
    stub = control.create_stub(
        "worker",
        workspace=workspace.id,
        config={
            "volumes": [
                {
                    "id": "models",
                    "mount_path": "/models",
                    "config": {
                        "bucket_name": "model-bucket",
                        "auth_mode": "secret_references",
                        "access_key": "MOUNT_ACCESS_KEY",
                        "secret_key": "MOUNT_SECRET_KEY",
                        "endpoint_url": "https://mount.local",
                        "region": "us-test-2",
                        "force_path_style": True,
                    },
                }
            ]
        },
    )
    container = isolated_services.containers.run(
        "worker-container",
        image="python:3.12",
        command="true",
        workspace_id=workspace.id,
        stub_id=stub.id,
    )

    credentials = WorkerCredentialService(
        isolated_services, storage_issuer=StoredWorkspaceStorageIssuer()
    ).vend(
        ContainerCredentialRequest(
            workspace_id=workspace.id,
            stub_id=stub.id,
            container_id=container.id,
            mount_credentials=[
                ContainerMountCredentialRequest(
                    mount_path="/models",
                    bucket_name="model-bucket",
                ),
                ContainerMountCredentialRequest(
                    mount_path="/volumes/models",
                    bucket_name="model-bucket",
                ),
            ],
        ),
        principal=WorkerCredentialPrincipal(
            workspace_id=workspace.id,
            token_kind=TokenKind.Worker,
        ),
    )

    assert [item.mount_path for item in credentials.mount_credentials] == [
        "/models",
        "/volumes/models",
    ]
    for item in credentials.mount_credentials:
        assert item.access_key == "mount-ak"
        assert item.secret_key == "mount-sk"
        assert item.endpoint_url == "https://mount.local"
        assert item.region == "us-test-2"
        assert item.force_path_style


def test_worker_credential_service_rejects_invalid_principal_and_assignment(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "workspace-a")
    stub = control.create_stub("worker", workspace=workspace.id)
    container = isolated_services.containers.run(
        "worker-container",
        image="python:3.12",
        command="true",
        workspace_id=workspace.id,
        stub_id=stub.id,
    )
    service = WorkerCredentialService(
        isolated_services, storage_issuer=StoredWorkspaceStorageIssuer()
    )
    request = ContainerCredentialRequest(
        workspace_id=workspace.id,
        stub_id=stub.id,
        container_id=container.id,
        gateway_token=True,
    )

    with pytest.raises(WorkerCredentialError, match="worker token is required"):
        service.vend(
            request,
            principal=WorkerCredentialPrincipal(
                workspace_id=workspace.id,
                token_kind=TokenKind.Workspace,
            ),
        )

    credentials = service.vend(
        request,
        principal=WorkerCredentialPrincipal(
            workspace_id="infrastructure-worker",
            token_kind=TokenKind.Worker,
        ),
    )
    assert any(item.startswith(f"{GATEWAY_TOKEN_ENV}=") for item in credentials.env)

    with pytest.raises(WorkerCredentialError, match="private worker token"):
        service.vend(
            request,
            principal=WorkerCredentialPrincipal(
                workspace_id="different",
                token_kind=TokenKind.WorkerPrivate,
            ),
        )

    with pytest.raises(WorkerCredentialError, match="workspace/stub"):
        service.vend(
            request.model_copy(update={"stub_id": "other-stub"}),
            principal=WorkerCredentialPrincipal(
                workspace_id=workspace.id,
                token_kind=TokenKind.Worker,
            ),
        )


def test_worker_credential_service_rejects_unavailable_secret_storage_and_mount(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "workspace-a")
    stub = control.create_stub("worker", workspace=workspace.id)
    container = isolated_services.containers.run(
        "worker-container",
        image="python:3.12",
        command="true",
        workspace_id=workspace.id,
        stub_id=stub.id,
    )
    service = WorkerCredentialService(
        isolated_services, storage_issuer=StoredWorkspaceStorageIssuer()
    )
    principal = WorkerCredentialPrincipal(
        workspace_id=workspace.id,
        token_kind=TokenKind.Worker,
    )

    with pytest.raises(WorkerCredentialError, match="secret 'MISSING'"):
        service.vend(
            ContainerCredentialRequest(
                workspace_id=workspace.id,
                stub_id=stub.id,
                container_id=container.id,
                secret_names=["MISSING"],
            ),
            principal=principal,
        )

    with pytest.raises(WorkerCredentialError, match="workspace storage"):
        service.vend(
            ContainerCredentialRequest(
                workspace_id=workspace.id,
                stub_id=stub.id,
                container_id=container.id,
                workspace_storage=True,
            ),
            principal=principal,
        )

    with pytest.raises(WorkerCredentialError, match="mount credentials"):
        service.vend(
            ContainerCredentialRequest(
                workspace_id=workspace.id,
                stub_id=stub.id,
                container_id=container.id,
                mount_credentials=[
                    ContainerMountCredentialRequest(
                        mount_path="/mnt/missing",
                        bucket_name="missing",
                    )
                ],
            ),
            principal=principal,
        )


def test_worker_credential_hydrator_applies_credentials_to_execution_context(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(
        control,
        "default",
        storage=WorkspaceStorageConfig(
            backend="s3",
            bucket="workspace-bucket",
            config={
                "endpoint_url": "https://s3.local",
                "access_key": "workspace-ak",
                "secret_key": "workspace-sk",
            },
        ),
    )
    isolated_services.secrets.set("API_TOKEN", "secret-value")
    isolated_services.secrets.set("MOUNT_ACCESS_KEY", "mount-ak")
    isolated_services.secrets.set("MOUNT_SECRET_KEY", "mount-sk")
    stub = control.create_stub(
        "worker",
        workspace=workspace.id,
        config={
            "mount_credentials": [
                {
                    "mount_path": "/mnt/data",
                    "bucket_name": "mount-bucket",
                    "auth_mode": "secret_references",
                    "access_key": "MOUNT_ACCESS_KEY",
                    "secret_key": "MOUNT_SECRET_KEY",
                    "prefix": "tenant-a/",
                    "endpoint_url": "https://mount.local",
                    "region": "us-test-2",
                    "force_path_style": True,
                }
            ]
        },
    )
    container = isolated_services.containers.run(
        "worker-container",
        image="python:3.12",
        command="true",
        workspace_id=workspace.id,
        stub_id=stub.id,
    )
    hydrator = WorkerCredentialHydrator(
        _credential_service(isolated_services),
        principal=WorkerCredentialPrincipal(
            workspace_id=workspace.id,
            token_kind=TokenKind.Worker,
        ),
    )

    hydrated = hydrator.hydrate_container_credentials(
        ContainerExecutionContext(
            request=ContainerRequestContext(
                container_id=container.id,
                workspace_id=workspace.id,
                stub_id=stub.id,
                env=["API_TOKEN=old"],
                secret_names=["API_TOKEN"],
                gateway_token_required=True,
                workspace_storage_required=True,
                mounts=[
                    RequestMount(
                        mount_path="/mnt/data",
                        mount_type=RequestMountType.MountPoint,
                        mountpoint_config=RequestMountPointConfig(
                            bucket_name="mount-bucket",
                            prefix="tenant-a/",
                            auth_mode=MountAuthMode.SecretReferences,
                        ),
                    )
                ],
            )
        )
    )

    assert "API_TOKEN=old" not in hydrated.request.env
    assert "API_TOKEN=secret-value" in hydrated.request.env
    assert any(item.startswith(f"{GATEWAY_TOKEN_ENV}=rt_") for item in hydrated.request.env)
    assert hydrated.request.workspace_storage_available
    assert hydrated.request.workspace_storage_credentials is not None
    assert hydrated.request.workspace_storage_credentials.bucket_name == "workspace-bucket"
    mount_config = hydrated.request.mounts[0].mountpoint_config
    assert mount_config is not None
    assert mount_config.auth_mode is MountAuthMode.SecretReferences
    assert mount_config.prefix == "tenant-a/"
    assert mount_config.access_key == "mount-ak"
    assert mount_config.secret_key == "mount-sk"
    assert mount_config.endpoint_url == "https://mount.local"
    assert mount_config.region == "us-test-2"
    assert mount_config.force_path_style
