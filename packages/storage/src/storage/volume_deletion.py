from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from database.repositories.identity import WorkspaceRepository
from database.repositories.storage import VolumeRepository
from observability.workspace_changes import WorkspaceChangePublisher
from shared.containers import LIVE_CONTAINER_STATUSES
from shared.errors import ConflictError, NotFoundError
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import to_utc, utc_now

from storage.context import StorageContext
from storage.volume_filesystem import VolumeFilesystem, VolumeNamespace
from storage.volume_metering import PersistentVolumeMeteringService

LOGGER = logging.getLogger(__name__)


class VolumeWorkerAbsence(Protocol):
    def is_absent(self, worker_id: str) -> bool: ...


class VolumeContainerShutdowns(Protocol):
    @property
    def durable_worker_absence(self) -> VolumeWorkerAbsence | None: ...


@dataclass(slots=True)
class VolumeDeletionService:
    context: StorageContext
    filesystem: VolumeFilesystem
    metering: PersistentVolumeMeteringService
    worker_absence: VolumeWorkerAbsence
    workspace_changes: WorkspaceChangePublisher | None = None

    def request(self, name: str, *, workspace_id: str) -> bool:
        with self.context.database.session() as session:
            WorkspaceRepository(session).lock_storage_accounting_owner(workspace_id)
            volumes = VolumeRepository(session)
            row = volumes.lock(name, workspace_id=workspace_id, allow_deleting=True)
            if row.deletion_requested_at is None:
                for container in volumes.unreleased_mounts(name, workspace_id=workspace_id):
                    worker_id = container.runtime_worker_id or container.worker_id
                    if container.status in LIVE_CONTAINER_STATUSES:
                        raise ConflictError(
                            f"stop container {container.id} before deleting volume {name}"
                        )
                    if worker_id and not self.worker_absence.is_absent(worker_id):
                        raise ConflictError(
                            f"container {container.id} has not released volume {name}"
                        )
                row.deletion_requested_at = utc_now()
        self._publish(workspace_id, name, WorkspaceChangeType.Updated)
        try:
            return self.finish(name, workspace_id=workspace_id)
        except Exception:
            LOGGER.exception(
                "volume deletion queued for retry",
                extra={"workspace_id": workspace_id, "volume_name": name},
            )
            return False

    def finish(self, name: str, *, workspace_id: str) -> bool:
        self.metering.finalize_volume_deletion(name, workspace_id=workspace_id)
        with self.context.database.session() as session:
            WorkspaceRepository(session).lock_storage_accounting_owner(workspace_id)
            volumes = VolumeRepository(session)
            try:
                row = volumes.lock(name, workspace_id=workspace_id, allow_deleting=True)
            except NotFoundError:
                return True
            if row.deletion_requested_at is None:
                raise ConflictError(f"volume {name} has no deletion request")
            self.filesystem.delete_volume(
                VolumeNamespace(workspace_id=workspace_id, volume_id=str(row.id))
            )
            volumes.retain_cleanup(row)
            session.delete(row)
        self._publish(workspace_id, name, WorkspaceChangeType.Deleted)
        return True

    def reconcile_due(self, *, now: datetime | None = None, limit: int = 100) -> None:
        with self.context.database.session() as session:
            targets = VolumeRepository(session).list_deletions(limit=limit)
        for workspace_id, name in targets:
            with self.context.database.session() as session:
                try:
                    row = VolumeRepository(session).lock(
                        name, workspace_id=workspace_id, allow_deleting=True
                    )
                except NotFoundError:
                    continue
                row.updated_at = utc_now()
            try:
                self.finish(name, workspace_id=workspace_id)
            except Exception:
                LOGGER.exception(
                    "volume deletion failed",
                    extra={"workspace_id": workspace_id, "volume_name": name},
                )
        observed_at = to_utc(now or utc_now())
        with self.context.database.session() as session:
            cleanup = VolumeRepository(session).list_cleanup(
                swept_before=observed_at - timedelta(hours=1), limit=limit
            )
        for workspace_id, volume_id in cleanup:
            try:
                with self.context.database.session() as session:
                    row = VolumeRepository(session).lock_cleanup(
                        volume_id, workspace_id=workspace_id
                    )
                    if row is None:
                        continue
                    row.swept_at = observed_at
                    try:
                        self.filesystem.delete_volume(
                            VolumeNamespace(workspace_id=workspace_id, volume_id=volume_id)
                        )
                    except Exception:
                        LOGGER.exception(
                            "unfenced volume write cleanup failed",
                            extra={"workspace_id": workspace_id, "volume_id": volume_id},
                        )
            except Exception:
                LOGGER.exception(
                    "unfenced volume write cleanup failed",
                    extra={"workspace_id": workspace_id, "volume_id": volume_id},
                )

    def _publish(self, workspace_id: str, name: str, change: WorkspaceChangeType) -> None:
        if self.workspace_changes is not None:
            self.workspace_changes.emit_change(
                workspace_id=workspace_id,
                topic=WorkspaceChangeTopic.StorageVolumes,
                change=change,
                resource_id=name,
            )
