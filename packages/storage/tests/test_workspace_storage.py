from __future__ import annotations

import hashlib
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import (
    ControlPlaneService,
    WorkspaceStorageAlreadyExistsError,
    WorkspaceStorageError,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.images import ImageArchiveRepository, ImageRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.app_identity import (
    IMAGE_BUILD_CONTEXT_BUCKET,
    SOURCE_PACKAGE_BUCKET,
    WORKSPACE_OBJECT_BUCKET,
)
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.identity import (
    TokenKind,
    TokenStatus,
    WorkspaceStatus,
    WorkspaceStorageConfig,
)
from shared.image_building.records import ImageRecord
from storage.service import OBJECT_SHA256_METADATA_KEY, ObjectStorage
from storage_client.s3 import S3ObjectInfo, S3ObjectStoreSettings
from tests.fakes import FakeObjectClient
from tests.service_fixtures import owned_workspace


@dataclass
class BucketClient(FakeObjectClient):
    settings: S3ObjectStoreSettings = field(
        default_factory=lambda: S3ObjectStoreSettings(
            bucket="lazycloud-objects",
            endpoint_url="http://storage:9000",
            region_name="us-test-1",
            access_key_id="default-access",
            secret_access_key="default-secret",
            force_path_style=True,
        )
    )
    fail_validate: bool = False
    created: list[str] = field(default_factory=list)
    validated: list[str] = field(default_factory=list)
    close_count: int = 0

    def create_bucket(self, bucket: str | None = None) -> None:
        self.created.append(bucket or self.settings.bucket)

    def validate_bucket_access(self, bucket: str | None = None) -> None:
        target = bucket or self.settings.bucket
        self.validated.append(target)
        if self.fail_validate:
            msg = f"denied: {target}"
            raise PermissionError(msg)

    def close(self) -> None:
        self.close_count += 1


@dataclass
class MetadataObjectClient(FakeObjectClient):
    object_metadata: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    fail_head: bool = False

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        result = super().put_bytes(
            key,
            data,
            bucket=bucket,
            content_type=content_type,
            metadata=metadata,
        )
        self.object_metadata[(result.bucket, key)] = dict(metadata or {})
        return result

    def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        result = super().put_file(
            key,
            source,
            bucket=bucket,
            content_type=content_type,
            metadata=metadata,
        )
        self.object_metadata[(result.bucket, key)] = dict(metadata or {})
        return result

    def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
        if self.fail_head:
            raise OSError("object store unavailable")
        result = super().head(key, bucket=bucket)
        return result.model_copy(
            update={"metadata": self.object_metadata.get((result.bucket, key), {})}
        )

    def delete(self, key: str, *, bucket: str | None = None) -> None:
        target_bucket = bucket or "default"
        super().delete(key, bucket=target_bucket)
        self.object_metadata.pop((target_bucket, key), None)


def test_workspace_create_sets_up_default_storage_and_primary_token(
    isolated_services: ApiServices,
) -> None:
    bucket_client = BucketClient()
    service = ControlPlaneService(
        isolated_services.context,
        workspace_storage_client=bucket_client,
    )

    owner = isolated_services.users.create(username="tenant-owner", password="tenant-owner-pass")

    created = service.create_workspace("tenant", owner_user_id=owner.id)
    workspace = service.get_workspace(created.workspace_id)

    assert created.workspace.storage.bucket == f"workspace-{created.workspace_id}"
    assert workspace.storage.bucket == f"workspace-{created.workspace_id}"
    assert workspace.storage.backend == "s3"
    # A dedicated bucket per workspace needs no prefix; it stays meaningful
    # only for a bucket the customer attaches themselves.
    assert workspace.storage.prefix == ""
    assert workspace.storage.config["endpoint_url"] == "http://storage:9000"
    assert workspace.storage.config["access_key"] == "default-access"
    assert bucket_client.created == [f"workspace-{created.workspace_id}"]
    assert bucket_client.validated == [f"workspace-{created.workspace_id}"]
    assert AuthService(isolated_services.context).authenticate(created.token).workspace_id == (
        created.workspace_id
    )


def test_workspace_storage_creation_validates_before_persisting(
    isolated_services: ApiServices,
) -> None:
    bucket_client = BucketClient(fail_validate=True)
    service = ControlPlaneService(
        isolated_services.context,
        workspace_storage_client=bucket_client,
    )
    workspace = owned_workspace(service, "broken")

    with pytest.raises(WorkspaceStorageError, match="unable to create workspace storage bucket"):
        service.create_workspace_storage(workspace.id)

    stored = service.get_workspace(workspace.id)
    assert stored.storage.bucket is None
    assert bucket_client.created == [f"workspace-{workspace.id}"]
    assert bucket_client.validated == [f"workspace-{workspace.id}"]


def test_external_workspace_storage_validates_and_rejects_duplicates(
    isolated_services: ApiServices,
) -> None:
    external_client = BucketClient()
    validated_configs: list[WorkspaceStorageConfig] = []

    def client_factory(storage: WorkspaceStorageConfig) -> BucketClient:
        validated_configs.append(storage)
        return external_client

    service = ControlPlaneService(
        isolated_services.context,
        workspace_storage_client_factory=client_factory,
    )
    workspace = owned_workspace(service, "tenant")
    storage = WorkspaceStorageConfig(
        backend="s3",
        bucket="external-bucket",
        config={
            "endpoint_url": "https://s3.example.test",
            "region": "us-east-1",
            "access_key": "external-access",
            "secret_key": "external-secret",
        },
    )

    updated = service.attach_external_workspace_storage(workspace.id, storage)

    assert updated.storage.bucket == "external-bucket"
    assert external_client.created == []
    assert external_client.validated == ["external-bucket"]
    assert external_client.close_count == 1
    assert validated_configs == [storage]
    with pytest.raises(WorkspaceStorageAlreadyExistsError, match="already exists"):
        service.attach_external_workspace_storage(workspace.id, storage)


def test_workspace_storage_api_keeps_token_active_after_cache_invalidation_hook(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
    client_stack: ExitStack,
) -> None:
    bucket_client = BucketClient()
    services = _services_with_object_storage(
        isolated_services,
        ObjectStorage(
            isolated_services.context,
            object_client=bucket_client,
        ),
        request,
    )
    control = ControlPlaneService(services.context)
    workspace = owned_workspace(control, "tenant")
    raw_token, token_record = AuthService(services.context).create_token(
        "tenant-storage",
        kind=TokenKind.WorkspacePrimary,
        workspace_id=workspace.id,
    )
    client = client_stack.enter_context(TestClient(create_app(services)))

    response = client.post(
        "/api/v1/workspaces/create-storage",
        headers=_auth(raw_token),
    )

    assert response.status_code == 201
    assert response.json()["storage"]["bucket"] == f"workspace-{workspace.id}"
    assert bucket_client.created == [f"workspace-{workspace.id}"]
    assert bucket_client.validated == [f"workspace-{workspace.id}"]
    assert AuthService(services.context).authenticate(raw_token).id == token_record.id
    assert AuthService(services.context).list_tokens()[0].status is TokenStatus.Active

    duplicate_raw_token, duplicate_record = AuthService(services.context).create_token(
        "tenant-storage-duplicate",
        kind=TokenKind.WorkspacePrimary,
        workspace_id=workspace.id,
    )
    duplicate = client.post(
        "/api/v1/workspaces/create-storage",
        headers=_auth(duplicate_raw_token),
    )

    assert duplicate.status_code == 400
    tokens = {token.id: token for token in AuthService(services.context).list_tokens()}
    assert tokens[token_record.id].status is TokenStatus.Active
    assert tokens[duplicate_record.id].status is TokenStatus.Active


def test_workspace_objects_with_same_logical_location_are_physically_isolated(
    isolated_services: ApiServices,
) -> None:
    client = MetadataObjectClient()
    storage = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="physical-objects",
    )
    control = ControlPlaneService(isolated_services.context)
    first = owned_workspace(control, "first-object-owner")
    second = owned_workspace(control, "second-object-owner")

    first_record = storage.put_bytes_for_workspace(
        workspace_id=first.id,
        bucket=WORKSPACE_OBJECT_BUCKET,
        key="models/shared.bin",
        data=b"first",
    )
    second_record = storage.put_bytes_for_workspace(
        workspace_id=second.id,
        bucket=WORKSPACE_OBJECT_BUCKET,
        key="models/shared.bin",
        data=b"second",
    )

    first_key = storage.physical_key_for_record(first_record)
    second_key = storage.physical_key_for_record(second_record)
    assert first_key != second_key
    assert first_record.path == f"s3://physical-objects/{first_key}"
    assert second_record.path == f"s3://physical-objects/{second_key}"
    assert storage.object_is_complete(first_record)
    assert storage.object_is_complete(second_record)

    with isolated_services.context.database.session() as session:
        workspaces = WorkspaceRepository(session)
        deleting = workspaces.lock_for_deletion(first.id)
        workspaces.mark_deleting(deleting)
    assert storage.delete_workspace_objects_for_deletion(first.id) == 1

    assert not client.exists(first_key, bucket="physical-objects")
    assert client.exists(second_key, bucket="physical-objects")
    assert (
        storage.read_bytes_for_workspace(
            workspace_id=second.id,
            bucket=WORKSPACE_OBJECT_BUCKET,
            key="models/shared.bin",
        )
        == b"second"
    )


def test_logical_object_purposes_share_one_physical_bucket_with_distinct_prefixes(
    isolated_services: ApiServices,
) -> None:
    client = MetadataObjectClient()
    storage = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="physical-objects",
    )
    workspace = owned_workspace(
        ControlPlaneService(isolated_services.context), "logical-object-purpose-owner"
    )

    records = tuple(
        storage.put_bytes_for_workspace(
            workspace_id=workspace.id,
            bucket=bucket,
            key="artifact.bin",
            data=bucket.encode(),
        )
        for bucket in (
            WORKSPACE_OBJECT_BUCKET,
            IMAGE_BUILD_CONTEXT_BUCKET,
            SOURCE_PACKAGE_BUCKET,
        )
    )

    assert {record.path.split("/", maxsplit=3)[2] for record in records} == {"physical-objects"}
    assert {(bucket, key) for bucket, key in client.objects if key.endswith("/artifact.bin")} == {
        (
            "physical-objects",
            f"workspaces/{workspace.id}/{purpose}/artifact.bin",
        )
        for purpose in (
            WORKSPACE_OBJECT_BUCKET,
            IMAGE_BUILD_CONTEXT_BUCKET,
            SOURCE_PACKAGE_BUCKET,
        )
    }


def test_immutable_file_replay_reuses_complete_object_and_repairs_missing_bytes(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    client = MetadataObjectClient()
    storage = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="physical-objects",
    )
    workspace = owned_workspace(
        ControlPlaneService(isolated_services.context), "immutable-object-owner"
    )
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"immutable payload")

    first = storage.put_file_for_workspace(
        workspace_id=workspace.id,
        bucket=WORKSPACE_OBJECT_BUCKET,
        key="artifacts/current.bin",
        source=source,
        overwrite=False,
    )
    replay = storage.put_file_for_workspace(
        workspace_id=workspace.id,
        bucket=WORKSPACE_OBJECT_BUCKET,
        key="artifacts/current.bin",
        source=source,
        overwrite=False,
    )

    assert replay == first
    assert len(client.file_uploads) == 1
    physical_key = storage.physical_key_for_record(first)
    client.delete(physical_key, bucket="physical-objects")
    assert not storage.object_is_complete(first)

    repaired = storage.put_file_for_workspace(
        workspace_id=workspace.id,
        bucket=WORKSPACE_OBJECT_BUCKET,
        key="artifacts/current.bin",
        source=source,
        overwrite=False,
    )

    assert repaired == first
    assert len(client.file_uploads) == 2
    assert storage.object_is_complete(repaired)

    source.write_bytes(b"different payload")
    with pytest.raises(ConflictError, match="object already exists"):
        storage.put_file_for_workspace(
            workspace_id=workspace.id,
            bucket=WORKSPACE_OBJECT_BUCKET,
            key="artifacts/current.bin",
            source=source,
            overwrite=False,
        )


def test_object_completeness_requires_exact_metadata_and_maps_store_outages(
    isolated_services: ApiServices,
) -> None:
    client = MetadataObjectClient()
    storage = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="physical-objects",
    )
    workspace = owned_workspace(
        ControlPlaneService(isolated_services.context), "object-completeness-owner"
    )
    record = storage.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=WORKSPACE_OBJECT_BUCKET,
        key="objects/exact.bin",
        data=b"exact",
    )
    physical_key = storage.physical_key_for_record(record)
    metadata_key = ("physical-objects", physical_key)

    assert storage.object_is_complete(record)
    client.object_metadata[metadata_key][OBJECT_SHA256_METADATA_KEY] = "wrong"
    assert not storage.object_is_complete(record)
    client.object_metadata[metadata_key][OBJECT_SHA256_METADATA_KEY] = record.sha256
    client.objects[metadata_key] = b"wrong-size"
    assert not storage.object_is_complete(record)
    client.fail_head = True
    with pytest.raises(UpstreamUnavailableError, match="completeness check failed"):
        storage.object_is_complete(record)


def _services_with_object_storage(
    isolated_services: ApiServices,
    object_storage: ObjectStorage,
    request: pytest.FixtureRequest,
) -> ApiServices:
    services = ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        object_storage=object_storage,
        volume_filesystem=isolated_services.volume_filesystem,
        redis_client=isolated_services.redis_client,
        binary_redis_client=isolated_services.binary_redis_client,
        owns_redis_client=False,
        owns_binary_redis_client=False,
        agent_binary_settings=isolated_services.agent_binary_settings,
    )
    request.addfinalizer(services.close)
    return services


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_workspace_deletion_preserves_a_published_archive_a_sibling_still_uses(
    isolated_services: ApiServices,
) -> None:
    """Deleting a tenant must not destroy bytes another tenant is authorized for.

    Archive facts used to live on each workspace's `images` row, so deletion took
    the shared bytes with it and then wedged on a `RESTRICT` foreign key onto
    `objects`. The archive is global now, so deletion should reach neither.
    """

    client = MetadataObjectClient()
    storage = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="physical-objects",
    )
    control = ControlPlaneService(isolated_services.context)
    leaving = owned_workspace(control, "archive-leaving-owner")
    staying = owned_workspace(control, "archive-staying-owner")
    image_id = "shared-image"
    archive_key = f"image-archives/{image_id}.rclip"
    client.put_bytes(archive_key, b"archive", bucket="image-archives")

    storage.put_bytes_for_workspace(
        workspace_id=leaving.id,
        bucket=WORKSPACE_OBJECT_BUCKET,
        key="models/owned.bin",
        data=b"leaving",
    )
    with isolated_services.context.database.session() as session:
        ImageArchiveRepository(session).reserve(
            image_id,
            bucket="image-archives",
            object_key=archive_key,
            size_bytes=len(b"archive"),
            sha256=hashlib.sha256(b"archive").hexdigest(),
        )
        images = ImageRepository(session)
        images.upsert(ImageRecord(workspace_id=leaving.id, image_id=image_id))
        images.upsert(ImageRecord(workspace_id=staying.id, image_id=image_id))

    with isolated_services.context.database.session() as session:
        workspaces = WorkspaceRepository(session)
        workspaces.mark_deleting(workspaces.lock_for_deletion(leaving.id))
    assert storage.delete_workspace_objects_for_deletion(leaving.id) == 1
    with isolated_services.context.database.session() as session:
        workspaces = WorkspaceRepository(session)
        purged = workspaces.purge_owned_records(leaving.id)
        deleted = workspaces.tombstone(workspaces.lock_for_deletion(leaving.id))

    assert purged.get("images") == 1
    assert deleted.status is WorkspaceStatus.Deleted
    assert client.exists(archive_key, bucket="image-archives")
    with isolated_services.context.database.session() as session:
        archives = ImageArchiveRepository(session)
        assert archives.get(image_id) is not None
        assert archives.get_authorized(image_id, workspace_id=leaving.id) is None
        surviving = archives.get_authorized(image_id, workspace_id=staying.id)
        assert surviving is not None
        assert surviving.object_key == archive_key
