from __future__ import annotations

from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.records.apps import AppRecord
from database.repositories.apps import AppRepository
from database.repositories.identity import SecretRepository, TokenRepository
from database.repositories.images import ImageRepository
from database.repositories.orchestration import ContainerRepository, WorkerRepository
from database.repositories.storage import VolumeRepository
from shared.compute_fleet import Worker
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import NotFoundError
from shared.identity import TokenKind
from shared.image_building.records import ImageRecord


def test_cross_workspace_reads_and_deletes_are_denied_by_construction(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owner = control.upsert_workspace("tenant-owner")
    intruder = control.upsert_workspace("tenant-intruder")

    app_id = str(uuid4())
    container_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        apps = AppRepository(session)
        apps.upsert(AppRecord(id=app_id, workspace_id=owner.id, name="owned-app"))
        secrets = SecretRepository(session)
        secrets.create(
            "owned-secret",
            "ciphertext",
            workspace_id=owner.id,
        )
        tokens = TokenRepository(session)
        token_id = tokens.create(
            name="owned-token",
            token_hash="hash",
            prefix="prefix1234",
            kind=TokenKind.Workspace,
            workspace_id=owner.id,
        )
        token_id = token_id.id
        volumes = VolumeRepository(session)
        volumes.create("owned-volume", workspace_id=owner.id)
        containers = ContainerRepository(session)
        containers.upsert(
            ContainerRecord(
                id=container_id,
                name="owned-container",
                image="registry/image:1",
                command=["sleep"],
                workspace_id=owner.id,
            )
        )

    with isolated_services.context.database.session() as session:
        apps = AppRepository(session)
        assert apps.get(app_id, workspace_id=intruder.id) is None
        assert apps.get(app_id, workspace_id=owner.id) is not None
        assert apps.list(workspace_id=intruder.id) == []

        secrets = SecretRepository(session)
        assert secrets.get("owned-secret", workspace_id=intruder.id) is None
        assert secrets.list(workspace_id=intruder.id) == []
        with pytest.raises(NotFoundError, match="secret not found"):
            secrets.delete("owned-secret", workspace_id=intruder.id)
        assert secrets.get("owned-secret", workspace_id=owner.id) is not None

        tokens = TokenRepository(session)
        assert tokens.get(token_id, workspace_id=intruder.id) is None
        assert tokens.list(workspace_id=intruder.id) == []
        assert tokens.delete(token_id, workspace_id=intruder.id) is False
        assert tokens.get(token_id, workspace_id=owner.id) is not None

        volumes = VolumeRepository(session)
        assert volumes.get("owned-volume", workspace_id=intruder.id) is None
        assert volumes.delete("owned-volume", workspace_id=intruder.id) is False
        assert volumes.get("owned-volume", workspace_id=owner.id) is not None

        containers = ContainerRepository(session)
        assert containers.get(container_id, workspace_id=intruder.id) is None
        assert containers.list(workspace_id=intruder.id) == []
        assert containers.records.delete(container_id, workspace_id=intruder.id) is False
        assert containers.get(container_id, workspace_id=owner.id) is not None

        # Explicit system access still sees the rows; the name marks the authority.
        assert apps.get_across_workspaces(app_id) is not None
        assert containers.get_across_workspaces(container_id) is not None


def test_image_archive_identity_is_exact_and_tenant_scoped(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owner = control.upsert_workspace("image-archive-owner")
    sibling = control.upsert_workspace("image-archive-sibling")
    image_id = "shared-image-name"
    archive_bucket = isolated_services.object_storage.default_bucket
    owner_archive = isolated_services.object_storage.reserve_for_workspace(
        workspace_id=owner.id,
        bucket=archive_bucket,
        key=f"image-builds/{owner.id}/{image_id}.rclip",
        size=1024,
        sha256="a" * 64,
        content_type="application/x-tar",
    )
    sibling_archive = isolated_services.object_storage.reserve_for_workspace(
        workspace_id=sibling.id,
        bucket=archive_bucket,
        key=f"image-builds/{sibling.id}/{image_id}.rclip",
        size=2048,
        sha256="b" * 64,
        content_type="application/x-tar",
    )

    with isolated_services.context.database.session() as session:
        images = ImageRepository(session)
        images.upsert(
            ImageRecord(
                workspace_id=owner.id,
                image_id=image_id,
                archive_object_id=owner_archive.id,
                archive_object_key=owner_archive.key,
                archive_size_bytes=owner_archive.size,
                archive_sha256=owner_archive.sha256,
            )
        )
        images.upsert(
            ImageRecord(
                workspace_id=sibling.id,
                image_id=image_id,
                archive_object_id=sibling_archive.id,
                archive_object_key=sibling_archive.key,
                archive_size_bytes=sibling_archive.size,
                archive_sha256=sibling_archive.sha256,
            )
        )

    with isolated_services.context.database.session() as session:
        images = ImageRepository(session)
        owner_image = images.get(image_id, workspace_id=owner.id)
        sibling_image = images.get(image_id, workspace_id=sibling.id)
        assert owner_image is not None
        assert owner_image.archive_object_id == owner_archive.id
        assert owner_image.archive_object_key == owner_archive.key
        assert owner_image.archive_size_bytes == owner_archive.size
        assert owner_image.archive_sha256 == owner_archive.sha256
        assert sibling_image is not None
        assert sibling_image.archive_object_id == sibling_archive.id
        assert sibling_image.archive_object_key == sibling_archive.key
        assert sibling_image.archive_size_bytes == sibling_archive.size
        assert sibling_image.archive_sha256 == sibling_archive.sha256
        assert images.archive_object_is_referenced(
            owner_archive.id,
            workspace_id=owner.id,
        )
        assert not images.archive_object_is_referenced(
            owner_archive.id,
            workspace_id=sibling.id,
        )
        assert images.delete(image_id, workspace_id=owner.id)
        assert images.get(image_id, workspace_id=owner.id) is None
        assert images.get(image_id, workspace_id=sibling.id) is not None


def test_container_shutdown_targets_include_only_active_workspace_rows(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = control.upsert_workspace("shutdown-target-owner")
    sibling = control.upsert_workspace("shutdown-target-sibling")
    compute_worker_id = str(uuid4())
    ids_by_name: dict[str, str] = {}

    with isolated_services.context.database.session() as session:
        WorkerRepository(session).upsert(
            Worker(id=compute_worker_id),
            workspace_id=workspace.id,
        )
        repository = ContainerRepository(session)
        for name, status, runtime_worker_id, worker_id in (
            ("pending", ContainerStatus.Pending, "", None),
            ("running-runtime", ContainerStatus.Running, "runtime-worker", compute_worker_id),
            ("running-compute", ContainerStatus.Running, "", compute_worker_id),
            ("exited", ContainerStatus.Exited, "stale-worker", None),
            ("failed", ContainerStatus.Failed, "stale-worker", None),
            ("stopped", ContainerStatus.Stopped, "stale-worker", None),
        ):
            container_id = str(uuid4())
            ids_by_name[name] = container_id
            repository.upsert(
                ContainerRecord(
                    id=container_id,
                    name=name,
                    image="",
                    command=[],
                    workspace_id=workspace.id,
                    runtime_worker_id=runtime_worker_id,
                    worker_id=worker_id,
                    status=status,
                )
            )
        repository.upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="sibling-running",
                image="",
                command=[],
                workspace_id=sibling.id,
                runtime_worker_id="sibling-worker",
                status=ContainerStatus.Running,
            )
        )

        targets = repository.list_active_shutdown_targets(workspace_id=workspace.id)

    assert {target.container_id: target.worker_id for target in targets} == {
        ids_by_name["pending"]: "",
        ids_by_name["running-runtime"]: "runtime-worker",
        ids_by_name["running-compute"]: compute_worker_id,
    }
