from __future__ import annotations

from dataclasses import dataclass

from database.repositories.identity import WorkspaceRepository
from database.repositories.images import ImageArchiveRepository
from database.repositories.storage import ObjectRepository, OwnedObjectRecord
from database.tables.execution import TaskTable
from database.tables.identity import WorkspaceStorageTable, WorkspaceTable
from database.tables.images import CheckpointTable, ImageArchiveTable, ImageBuildTable
from database.tables.orchestration import ContainerTable
from database.tables.storage import ObjectTable
from shared.identity import WorkspaceRecord, WorkspaceStorageConfig
from shared.image_building.records import ImageArchiveRecord
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ObjectStorageSnapshot:
    workspaces: tuple[WorkspaceRecord, ...]
    objects: tuple[OwnedObjectRecord, ...]
    archives: tuple[ImageArchiveRecord, ...]
    runtime_paths: tuple[str, ...]


@dataclass(slots=True)
class ObjectStorageMigrationRepository:
    session: Session

    def snapshot(self) -> ObjectStorageSnapshot:
        if self.session.scalar(select(WorkspaceStorageTable.id).limit(1)) is not None:
            raise RuntimeError("legacy workspace_storage rows require an explicit migration")
        for table, active in (
            (TaskTable, ("pending", "running", "retry")),
            (ContainerTable, ("pending", "running")),
            (ImageBuildTable, ("pending", "running")),
            (CheckpointTable, ("pending",)),
        ):
            if self.session.scalar(select(table.id).where(table.status.in_(active)).limit(1)):
                raise RuntimeError(f"drain active {table.__tablename__} before storage migration")
        workspaces = tuple(WorkspaceRepository(self.session).list())
        objects = tuple(
            OwnedObjectRecord(workspace.id, record)
            for workspace in workspaces
            for record in ObjectRepository(self.session).list_for_workspace_deletion(workspace.id)
        )
        for owned in objects:
            record = owned.record
            if record.write_claim_id or record.write_target or record.cleanup_kind:
                raise RuntimeError(
                    f"finish object writes and cleanup before migration: {record.id}"
                )
        archive_repository = ImageArchiveRepository(self.session)
        archives: list[ImageArchiveRecord] = []
        for image_id in self.session.scalars(select(ImageArchiveTable.image_id)):
            record = archive_repository.get(image_id)
            if record is None or record.cleanup_claimed_at is not None:
                raise RuntimeError(f"finish image archive cleanup before migration: {image_id}")
            archives.append(record)
        paths = tuple(
            path
            for row in self.session.scalars(select(ImageBuildTable))
            for path in (
                row.archive_path_value,
                row.manifest_path_value,
                row.dockerfile_path_value,
                row.cache_manifest_path_value,
            )
            if path
        )
        checkpoint_paths = tuple(
            value
            for row in self.session.scalars(select(CheckpointTable))
            for value in (row.origin_key, row.payload.get("remote_key"))
            if isinstance(value, str)
        )
        return ObjectStorageSnapshot(workspaces, objects, tuple(archives), paths + checkpoint_paths)

    def lock_writers(self) -> None:
        # Keep these locks through the copy and commit. A restarted controller must
        # not create a new location after the migration enumerates durable records.
        self.session.execute(
            text(
                "LOCK TABLE workspaces, workspace_storage, objects, image_archives, "
                "tasks, containers, image_builds, checkpoints "
                "IN SHARE ROW EXCLUSIVE MODE NOWAIT"
            )
        )

    def relocate_workspace(self, workspace_id: str, storage: WorkspaceStorageConfig) -> None:
        row = self.session.get(WorkspaceTable, workspace_id)
        if row is None:
            raise RuntimeError(f"workspace disappeared during migration: {workspace_id}")
        self.session.execute(
            update(WorkspaceTable)
            .where(WorkspaceTable.id == workspace_id)
            .values(
                payload={**row.payload, "storage": storage.model_dump(mode="json")},
                updated_at=row.updated_at,
            )
        )

    def relocate_object(self, object_id: str, *, bucket: str, path: str) -> None:
        row = self.session.get(ObjectTable, object_id)
        if row is None:
            raise RuntimeError(f"object disappeared during migration: {object_id}")
        self.session.execute(
            update(ObjectTable)
            .where(ObjectTable.id == object_id)
            .values(
                bucket=bucket,
                path=path,
                payload={**row.payload, "bucket": bucket, "path": path},
                updated_at=row.updated_at,
            )
        )

    def relocate_archive(self, image_id: str, *, bucket: str) -> None:
        row = self.session.scalars(
            select(ImageArchiveTable).where(ImageArchiveTable.image_id == image_id)
        ).one()
        self.session.execute(
            update(ImageArchiveTable)
            .where(ImageArchiveTable.id == row.id)
            .values(
                bucket=bucket,
                updated_at=row.updated_at,
            )
        )
