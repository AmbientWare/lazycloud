from __future__ import annotations

from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.records.apps import AppRecord
from database.repositories.apps import AppRepository
from database.repositories.identity import SecretRepository, TokenRepository
from database.repositories.images import ImageArchiveRepository, ImageRepository
from database.repositories.orchestration import ContainerRepository, WorkerRepository
from database.repositories.storage import VolumeRepository
from shared.compute_fleet import Worker
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import NotFoundError
from shared.identity import TokenKind
from shared.image_building.records import ImageRecord
from shared.timestamps import utc_now
from tests.service_fixtures import owned_workspace


def test_cross_workspace_reads_and_deletes_are_denied_by_construction(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owner = owned_workspace(control, "tenant-owner")
    intruder = owned_workspace(control, "tenant-intruder")

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
        assert tokens.revoke(token_id, workspace_id=intruder.id, now=utc_now()) is None
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


def test_one_global_archive_serves_every_authorized_workspace(
    isolated_services: ApiServices,
) -> None:
    """Two workspaces share one archive, and losing one leaves the other resolving it.

    This is the data-loss case the global archive exists to fix: the archive is
    keyed on the image id alone, so a workspace reaches it only through its own
    `images` row, and deleting that row must free nothing the sibling still needs.
    """

    control = ControlPlaneService(isolated_services.context)
    owner = owned_workspace(control, "image-archive-owner")
    sibling = owned_workspace(control, "image-archive-sibling")
    stranger = owned_workspace(control, "image-archive-stranger")
    image_id = "shared-image-name"
    manifest_digest = "sha256:" + "c" * 64
    registry_ref = f"registry.example.com/workloads@{manifest_digest}"

    with isolated_services.context.database.session() as session:
        archives = ImageArchiveRepository(session)
        archive, reserved = archives.reserve(
            image_id,
            bucket="image-archives",
            object_key=f"image-archives/{image_id}.rclip",
            size_bytes=1024,
            sha256="a" * 64,
            registry_ref=registry_ref,
            manifest_digest=manifest_digest,
            architecture="amd64",
            format_version=2,
        )
        assert reserved
        again, reserved_again = archives.reserve(
            image_id,
            bucket="image-archives",
            object_key=f"image-archives/{image_id}.rclip",
            size_bytes=2048,
            sha256="b" * 64,
            registry_ref=f"registry.example.com/workloads@sha256:{'d' * 64}",
            manifest_digest="sha256:" + "d" * 64,
            architecture="arm64",
            format_version=2,
        )
        assert not reserved_again
        assert again.id == archive.id
        assert again.sha256 == "a" * 64

        images = ImageRepository(session)
        images.upsert(ImageRecord(workspace_id=owner.id, image_id=image_id))
        images.upsert(ImageRecord(workspace_id=sibling.id, image_id=image_id))

    with isolated_services.context.database.session() as session:
        archives = ImageArchiveRepository(session)
        for workspace in (owner, sibling):
            resolved = archives.get_authorized(image_id, workspace_id=workspace.id)
            assert resolved is not None
            assert resolved.id == archive.id
        assert archives.get_authorized(image_id, workspace_id=stranger.id) is None

        assert ImageRepository(session).delete(image_id, workspace_id=owner.id)

    with isolated_services.context.database.session() as session:
        archives = ImageArchiveRepository(session)
        assert archives.get_authorized(image_id, workspace_id=owner.id) is None
        surviving = archives.get_authorized(image_id, workspace_id=sibling.id)
        assert surviving is not None
        assert surviving.id == archive.id


def test_container_shutdown_targets_include_only_active_workspace_rows(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "shutdown-target-owner")
    sibling = owned_workspace(control, "shutdown-target-sibling")
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
