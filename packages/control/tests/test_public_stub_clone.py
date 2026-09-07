from __future__ import annotations

import hashlib
from contextlib import ExitStack
from pathlib import Path

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.repositories.identity import SecretRepository
from database.repositories.storage import ObjectRepository, VolumeRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from pydantic import JsonValue, TypeAdapter
from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.identity import AuthScope, TokenKind
from storage.service import ObjectStorage
from storage_client.s3 import S3ObjectInfo, S3PresignedUpload
from tests.service_fixtures import owned_workspace

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def test_public_stub_config_allows_public_and_same_workspace_private_only(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owner = owned_workspace(control, "owner")
    other = owned_workspace(control, "other")
    private_stub = control.create_stub(
        "private-api",
        workspace=owner.id,
        public=False,
        config={"runtime": {"cpu": 1}},
    )
    public_stub = control.create_stub(
        "public-api",
        workspace=owner.id,
        public=True,
        config={"runtime": {"cpu": 2}},
    )
    owner_token = _workspace_token(isolated_services, owner.id, "owner-token")
    other_token = _workspace_token(isolated_services, other.id, "other-token")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    public_response = client.get(f"/api/v1/stubs/{public_stub.id}/config")
    owner_private_public_route = client.get(
        f"/api/v1/stubs/{private_stub.id}/config",
        headers=_auth(owner_token),
    )
    owner_private_scoped = client.get(
        f"/api/v1/stubs/{private_stub.id}",
        headers=_auth(owner_token),
    )
    other_private = client.get(
        f"/api/v1/stubs/{private_stub.id}/config",
        headers=_auth(other_token),
    )
    anonymous_private = client.get(f"/api/v1/stubs/{private_stub.id}/config")

    assert public_response.status_code == 200
    public_payload = _JSON_OBJECT_ADAPTER.validate_json(public_response.content)
    public_runtime = public_payload["runtime"]
    assert isinstance(public_runtime, dict)
    assert public_runtime["cpu"] == 2
    assert owner_private_public_route.status_code == 404
    assert owner_private_scoped.status_code == 200
    private_payload = _JSON_OBJECT_ADAPTER.validate_json(owner_private_scoped.content)
    private_config = private_payload["config"]
    assert isinstance(private_config, dict)
    private_runtime = private_config["runtime"]
    assert isinstance(private_runtime, dict)
    assert private_runtime["cpu"] == 1
    assert other_private.status_code == 404
    assert anonymous_private.status_code == 404


def test_public_clone_copies_local_object_and_remaps_target_workspace_refs(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    tmp_path: Path,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owner = owned_workspace(control, "clone-owner")
    target = owned_workspace(control, "clone-target")
    source = control.create_stub("shared", workspace=owner.id, public=True, kind=StubKind.Function)
    with isolated_services.context.database.session() as session:
        SecretRepository(session).create(
            "TARGET_SECRET",
            "encrypted",
            workspace_id=target.id,
        )
    source_file = tmp_path / "package.bin"
    source_bytes = b"package payload"
    source_file.write_bytes(source_bytes)
    source_object = _create_object(
        isolated_services,
        workspace_id=owner.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key=f"sources/{source.id}",
        path=str(source_file),
        content=source_bytes,
        metadata={"stub_id": source.id, "workspace_id": owner.id},
    )
    control.create_stub(
        "shared",
        workspace=owner.id,
        public=True,
        config={
            "object_id": source_object.id,
            "secrets": ["TARGET_SECRET", "OWNER_ONLY"],
            "volumes": [{"name": "models", "id": "source-volume", "path": "/private/models"}],
            "runtime": {"cpu": 1},
        },
    )
    token = _workspace_token(isolated_services, target.id, "clone-target-token")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.post(
        f"/api/v1/stubs/{source.id}/clone",
        json={"workspace": target.id},
        headers=_auth(token),
    )

    assert response.status_code == 201, response.text
    payload = _JSON_OBJECT_ADAPTER.validate_json(response.content)
    cloned = payload["cloned_stub"]
    assert isinstance(cloned, dict)
    copied_objects = payload["copied_objects"]
    assert isinstance(copied_objects, list)
    assert copied_objects
    copied_object_id = copied_objects[0]
    assert isinstance(copied_object_id, str)
    assert cloned["workspace_id"] == target.id
    cloned_config = cloned["config"]
    assert isinstance(cloned_config, dict)
    assert cloned_config["object_id"] == copied_object_id
    assert cloned_config["secrets"] == ["TARGET_SECRET"]
    with isolated_services.context.database.session() as session:
        target_volume = VolumeRepository(session).get("models", workspace_id=target.id)
        copied = next(
            (
                item
                for item in ObjectRepository(session).list(workspace_id=target.id)
                if item.id == copied_object_id
            ),
            None,
        )
    assert target_volume is not None
    cloned_volumes = cloned_config["volumes"]
    assert isinstance(cloned_volumes, list) and cloned_volumes
    cloned_volume = cloned_volumes[0]
    assert isinstance(cloned_volume, dict)
    assert cloned_volume["id"] == target_volume.id
    assert cloned_volume["name"] == target_volume.name
    assert cloned_volume.get("path", "") == ""
    assert copied is not None
    cloned_id = cloned["id"]
    assert isinstance(cloned_id, str)
    assert copied.metadata["stub_id"] == cloned_id
    assert Path(copied.path).read_bytes() == source_bytes


def test_cross_workspace_private_clone_is_denied(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owner = owned_workspace(control, "private-owner")
    other = owned_workspace(control, "private-other")
    source = control.create_stub("private", workspace=owner.id, public=False)
    token = _workspace_token(isolated_services, other.id, "private-other-token")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.post(
        f"/api/v1/stubs/{source.id}/clone",
        json={"workspace": other.id},
        headers=_auth(token),
    )

    assert response.status_code == 404


def test_deployment_package_download_streams_local_file_and_redirects_presigned(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "packages")
    local_stub = control.create_stub("local-package", workspace=workspace.id)
    remote_stub = control.create_stub("remote-package", workspace=workspace.id)
    local_file = tmp_path / "local.pkg"
    local_bytes = b"local deployment package"
    local_file.write_bytes(local_bytes)
    object_client = _PresignedObjectClient()
    object_storage = ObjectStorage(isolated_services.context, object_client=object_client)
    remote_bucket = object_storage.physical_bucket(SOURCE_PACKAGE_BUCKET)
    remote_key = object_storage.physical_key_for_workspace(
        workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="remote.pkg",
    )
    _create_object(
        isolated_services,
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="local.pkg",
        path=str(local_file),
        content=local_bytes,
        metadata={"stub_id": local_stub.id, "workspace_id": workspace.id},
    )
    _create_object(
        isolated_services,
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="remote.pkg",
        path=f"s3://{remote_bucket}/{remote_key}",
        content=b"remote package",
        metadata={"stub_id": remote_stub.id, "workspace_id": workspace.id},
    )
    object_client.put_bytes(remote_key, b"remote package", bucket=remote_bucket)
    services = _services_with_object_storage(isolated_services, object_storage, request)
    token = _workspace_token(services, workspace.id, "package-token")
    client = client_stack.enter_context(TestClient(create_app(services)))

    local_response = client.get(
        f"/api/v1/deployments/{local_stub.id}/download",
        headers=_auth(token),
    )
    remote_response = client.get(
        f"/api/v1/deployments/{remote_stub.id}/download",
        headers=_auth(token),
        follow_redirects=False,
    )

    assert local_response.status_code == 200
    assert local_response.content == local_bytes
    assert "local.pkg" in local_response.headers["content-disposition"]
    assert remote_response.status_code == 307
    assert (
        remote_response.headers["location"]
        == f"https://objects.example/{remote_bucket}/{remote_key}"
    )
    assert object_client.presigned == [(remote_bucket, remote_key, 600)]
    assert object_client.read_bytes(remote_key, bucket=remote_bucket) == b"remote package"


def _services_with_object_storage(
    isolated_services: ApiServices,
    object_storage: ObjectStorage,
    request: pytest.FixtureRequest,
) -> ApiServices:
    services = ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        workspace_storage_issuer=isolated_services.workspace_storage_issuer,
        object_storage=object_storage,
        volume_filesystem=isolated_services.volume_filesystem,
        redis_client=isolated_services.redis_client,
        binary_redis_client=isolated_services.binary_redis_client,
        async_io=isolated_services.require_async_io(),
        owns_redis_client=False,
        owns_binary_redis_client=False,
    )
    request.addfinalizer(services.close)
    return services


def _workspace_token(isolated_services: ApiServices, workspace_id: str, name: str) -> str:
    token, _ = AuthService(isolated_services.context).create_token(
        name,
        scopes=[AuthScope.Read.value, AuthScope.Write.value],
        kind=TokenKind.Workspace,
        workspace_id=workspace_id,
    )
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_object(
    isolated_services: ApiServices,
    *,
    workspace_id: str,
    bucket: str,
    key: str,
    path: str,
    content: bytes,
    metadata: dict[str, str],
):
    with isolated_services.context.database.session() as session:
        return ObjectRepository(session).records.create(
            {
                "bucket": bucket,
                "key": key,
                "path": path,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_type": "application/octet-stream",
                "metadata": metadata,
            },
            workspace_id=workspace_id,
        )


class _PresignedObjectClient:
    def __init__(self) -> None:
        self.presigned: list[tuple[str, str, int]] = []
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        _ = content_type, metadata
        target_bucket = bucket or "default"
        self.objects[(target_bucket, key)] = data
        return S3ObjectInfo(bucket=target_bucket, key=key, size=len(data))

    def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        return self.put_bytes(
            key,
            Path(source).read_bytes(),
            bucket=bucket,
            content_type=content_type,
            metadata=metadata,
        )

    def read_bytes(
        self, key: str, *, bucket: str | None = None, max_bytes: int | None = None
    ) -> bytes:
        return self.objects[(bucket or "default", key)][:max_bytes]

    def download_file(
        self,
        key: str,
        target: str | Path,
        *,
        bucket: str | None = None,
    ) -> S3ObjectInfo:
        data = self.read_bytes(key, bucket=bucket)
        Path(target).write_bytes(data)
        return S3ObjectInfo(bucket=bucket or "default", key=key, size=len(data))

    def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
        data = self.read_bytes(key, bucket=bucket)
        return S3ObjectInfo(bucket=bucket or "default", key=key, size=len(data))

    def exists(self, key: str, *, bucket: str | None = None) -> bool:
        return (bucket or "default", key) in self.objects

    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        target_bucket = bucket or "default"
        self.presigned.append((target_bucket, key, expires_seconds))
        return f"https://objects.example/{target_bucket}/{key}"

    def generate_presigned_put_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
    ) -> str:
        _ = content_length, content_type
        target_bucket = bucket or "default"
        return f"https://objects.example/{target_bucket}/{key}?expires={expires_seconds}"

    def generate_presigned_put(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        checksum_sha256: str = "",
    ) -> S3PresignedUpload:
        target_bucket = bucket or "default"
        return S3PresignedUpload(
            url=f"https://objects.example/{target_bucket}/{key}?expires={expires_seconds}",
            headers={
                "content-length": str(content_length),
                "content-type": content_type,
                **({"x-amz-checksum-sha256": checksum_sha256} if checksum_sha256 else {}),
                **{f"x-amz-meta-{name}": value for name, value in (metadata or {}).items()},
            },
        )

    def delete(self, key: str, *, bucket: str | None = None) -> None:
        self.objects.pop((bucket or "default", key), None)
