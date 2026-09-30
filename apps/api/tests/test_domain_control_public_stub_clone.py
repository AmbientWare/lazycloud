from __future__ import annotations

import hashlib
from contextlib import ExitStack
from pathlib import Path

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from apps.api.tests.runtime import services_with_object_storage
from control.service import ControlServices
from database.records.apps import StubKind
from database.repositories.identity import SecretRepository
from database.repositories.storage import ObjectRepository, VolumeRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from pydantic import JsonValue, TypeAdapter
from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.errors import InvalidInputError
from shared.http.apps import StubCloneResponse
from shared.http.stubs import PublicStubConfigResponse, StubResponse
from shared.identity import AuthScope, TokenKind
from shared.objects import ObjectWriteCommand
from storage.service import ObjectStorage
from tests.fakes import FakeObjectClient
from tests.workspaces import owned_workspace

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def test_public_stub_config_allows_public_and_same_workspace_private_only(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        control = ControlServices.create(
            isolated_services.context,
        )
        owner = owned_workspace(control, "owner")
        other = owned_workspace(control, "other")
        private_stub = control.stubs.create_stub(
            "private-api",
            workspace=owner.id,
            public=False,
            config={"runtime": {"cpu": 1}},
        )
        public_stub = control.stubs.create_stub(
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
        public = PublicStubConfigResponse.model_validate_json(public_response.content)
        assert public.runtime is not None and public.runtime.cpu == 2
        assert owner_private_public_route.status_code == 404
        assert owner_private_scoped.status_code == 200
        private = StubResponse.model_validate_json(owner_private_scoped.content)
        assert private.config.runtime is not None and private.config.runtime.cpu == 1
        assert other_private.status_code == 404
        assert anonymous_private.status_code == 404


def test_public_clone_copies_local_object_and_remaps_target_workspace_refs(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    with ExitStack() as client_stack:
        control = ControlServices.create(
            isolated_services.context,
        )
        owner = owned_workspace(control, "clone-owner")
        target = owned_workspace(control, "clone-target")
        source = control.stubs.create_stub(
            "shared", workspace=owner.id, public=True, kind=StubKind.Function
        )
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
        source = control.stubs.create_stub(
            "shared",
            workspace=owner.id,
            public=True,
            config={
                "object_id": source_object.id,
                "secrets": ["TARGET_SECRET", "OWNER_ONLY"],
                "volumes": [{"name": "models", "id": "source-volume", "path": "/private/models"}],
                "runtime": {"cpu": 1, "gpu": ["T4"]},
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
        payload = StubCloneResponse.model_validate_json(response.content)
        cloned = payload.cloned_stub
        (copied_object_id,) = payload.copied_objects
        assert cloned.workspace_id == target.id
        assert cloned.config.object_id == copied_object_id
        assert cloned.config.secrets == ["TARGET_SECRET"]
        assert cloned.config.runtime is not None and cloned.config.runtime.gpu == ["T4"]
        with isolated_services.context.database.session() as session:
            target_volume = VolumeRepository(session).get("models", workspace_id=target.id)
            copied = ObjectRepository(session).get(copied_object_id, workspace_id=target.id)
        assert target_volume is not None
        (cloned_volume,) = cloned.config.volumes
        assert cloned_volume.id == target_volume.id
        assert cloned_volume.name == target_volume.name
        assert cloned_volume.path == ""
        assert copied is not None
        assert copied.metadata["stub_id"] == cloned.id
        downloaded = tmp_path / "cloned.bin"
        isolated_services.object_storage.download_by_id_for_workspace(
            copied.id, downloaded, workspace_id=target.id
        )
        assert downloaded.read_bytes() == source_bytes


@pytest.mark.parametrize("failure", ["copy", "registration"])
def test_failed_clone_removes_its_copies_and_rolls_back_owned_records(
    isolated_services: ApiServices,
    tmp_path: Path,
    failure: str,
) -> None:
    control = isolated_services.control_plane_service
    owner = owned_workspace(control, "rollback-owner")
    target = owned_workspace(control, "rollback-target")
    source = control.stubs.create_stub(
        "invalid app name" if failure == "registration" else "copy-failure",
        workspace=owner.id,
        public=True,
        config={"volumes": [{"name": "clone-volume"}]},
    )
    if failure == "copy":
        _create_object(
            isolated_services,
            workspace_id=owner.id,
            bucket=SOURCE_PACKAGE_BUCKET,
            key="missing",
            path=str(tmp_path / "missing"),
            content=b"missing",
            metadata={"stub_id": source.id},
        )
    data = tmp_path / "source"
    data.write_bytes(b"source bytes")
    _create_object(
        isolated_services,
        workspace_id=owner.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="source",
        path=str(data),
        content=data.read_bytes(),
        metadata={"stub_id": source.id},
    )
    sibling = isolated_services.object_storage.put_file_for_workspace(
        workspace_id=target.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sibling",
        source=data,
        overwrite=False,
    )
    with pytest.raises(FileNotFoundError if failure == "copy" else InvalidInputError):
        control.cloning.clone_stub(source.id, apps=isolated_services.apps, workspace=target.id)
    assert control.stubs.list_stubs(workspace=target.id) == []
    assert isolated_services.apps.list(workspace=target.id) == []
    with isolated_services.context.database.session() as session:
        assert VolumeRepository(session).list(workspace_id=target.id) == []
        assert [record.id for record in ObjectRepository(session).list(workspace_id=target.id)] == [
            sibling.id
        ]
    downloaded = tmp_path / "sibling"
    isolated_services.object_storage.download_by_id_for_workspace(
        sibling.id, downloaded, workspace_id=target.id
    )
    assert downloaded.read_bytes() == b"source bytes"


def test_cross_workspace_private_clone_is_denied(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        control = ControlServices.create(
            isolated_services.context,
        )
        owner = owned_workspace(control, "private-owner")
        other = owned_workspace(control, "private-other")
        source = control.stubs.create_stub("private", workspace=owner.id, public=False)
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
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    with ExitStack() as client_stack:
        control = ControlServices.create(
            isolated_services.context,
        )
        workspace = owned_workspace(control, "packages")
        local_stub = control.stubs.create_stub("local-package", workspace=workspace.id)
        remote_stub = control.stubs.create_stub("remote-package", workspace=workspace.id)
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
        services = services_with_object_storage(isolated_services, object_storage, request)
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
        assert object_client.read_bytes(remote_key, bucket=remote_bucket) == b"remote package"


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
        return ObjectRepository(session).reserve(
            ObjectWriteCommand(
                bucket=bucket,
                key=key,
                path=path,
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                content_type="application/octet-stream",
                metadata=metadata,
            ),
            workspace_id=workspace_id,
            overwrite=False,
        )


class _PresignedObjectClient(FakeObjectClient):
    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        return f"https://objects.example/{bucket or 'default'}/{key}"
