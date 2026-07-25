from shared.mounts import MountAuthMode
from worker.tools import (
    ContainerCredentialContext,
    ContainerMount,
    ContainerMountKind,
    build_container_credential_request,
    has_container_credential_request,
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
