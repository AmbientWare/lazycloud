import pytest
from shared.mounts import MountAuthMode
from shared.shell_protocol import SHELL_AUTH_PASSWORD_ENV, SHELL_AUTH_USERNAME_ENV
from worker.tools import (
    ContainerCredentialContext,
    ContainerCredentials,
    ContainerMount,
    ContainerMountKind,
    apply_container_credentials,
    build_container_credential_request,
    has_container_credential_request,
)


def test_credential_hydration_preserves_shell_auth_and_rejects_reserved_secrets() -> None:
    existing = [f"{SHELL_AUTH_USERNAME_ENV}=shell-user", f"{SHELL_AUTH_PASSWORD_ENV}=shell-auth"]
    result = apply_container_credentials(
        existing_env=existing,
        mounts=[],
        credentials=ContainerCredentials(env=["USERNAME=workload-user", "PASSWORD=workload-auth"]),
    )
    assert set(result.env) == {*existing, "USERNAME=workload-user", "PASSWORD=workload-auth"}
    with pytest.raises(ValueError, match="reserved for shell authentication"):
        apply_container_credentials(
            existing_env=existing,
            mounts=[],
            credentials=ContainerCredentials(env=[f"{SHELL_AUTH_PASSWORD_ENV}=replacement"]),
        )


def test_ambient_bucket_mount_does_not_request_credentials() -> None:
    request = build_container_credential_request(
        ContainerCredentialContext(
            workspace_id="workspace-a",
            stub_id="stub-a",
            container_id="container-a",
            mounts=[
                ContainerMount(
                    mount_path="/volumes/data",
                    bucket_name="customer-data",
                    kind=ContainerMountKind.MountPoint,
                    auth_mode=MountAuthMode.Ambient,
                )
            ],
        )
    )

    assert request.mount_credentials == []
    assert not has_container_credential_request(request)


def test_secret_reference_bucket_mount_requests_credentials_once() -> None:
    mount = ContainerMount(
        mount_path="/volumes/data",
        bucket_name="customer-data",
        kind=ContainerMountKind.MountPoint,
        auth_mode=MountAuthMode.SecretReferences,
    )
    request = build_container_credential_request(
        ContainerCredentialContext(
            workspace_id="workspace-a",
            stub_id="stub-a",
            container_id="container-a",
            mounts=[mount, mount],
        )
    )

    assert [item.credential_key for item in request.mount_credentials] == [
        "/volumes/data:customer-data"
    ]
    assert has_container_credential_request(request)
