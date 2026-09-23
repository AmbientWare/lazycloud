"""Server-side disk leases for the worker that runs a disk's container."""

from __future__ import annotations

from dataclasses import dataclass

from database.repositories.apps import StubRepository
from identity.auth import AuthorizationDeniedError
from shared.containers import ContainerRecord
from shared.errors import NotFoundError
from storage.disks import DiskPublication, DiskService
from worker.durable_disk_records import (
    DiskAcquirePayload,
    DiskAcquireResult,
    DiskChainLayer,
    DiskPublishPayload,
    DiskPublishResult,
    DiskReleasePayload,
)

from database import DatabaseClient


@dataclass(slots=True)
class WorkerDiskLeaseService:
    """Leases a disk only to a container that declares it, in the disk's own workspace.

    The caller has already proven the worker was assigned the container. What is
    left to prove is that the container may write this disk at all: the id in the
    payload is the worker's claim, and a worker holding one tenant's container
    must not reach another tenant's disk by naming it.
    """

    database: DatabaseClient
    disks: DiskService

    def acquire(
        self, payload: DiskAcquirePayload, *, container: ContainerRecord, worker_id: str
    ) -> DiskAcquireResult:
        self._authorize(payload.disk_id, container=container)
        acquisition = self.disks.acquire(
            payload.disk_id, container_id=container.id, worker_id=worker_id
        )
        return DiskAcquireResult(
            disk_id=acquisition.disk_id,
            lease_token=acquisition.lease_token,
            size_bytes=acquisition.size_bytes,
            generation=acquisition.generation,
            chain=[
                DiskChainLayer(
                    generation=link.generation,
                    manifest_key=link.manifest_key,
                    manifest_sha256=link.manifest_sha256,
                )
                for link in acquisition.chain
            ],
        )

    def publish(
        self, payload: DiskPublishPayload, *, container: ContainerRecord
    ) -> DiskPublishResult:
        self._authorize(payload.disk_id, container=container)
        generation = self.disks.publish(
            DiskPublication(
                disk_id=payload.disk_id,
                container_id=container.id,
                lease_token=payload.lease_token,
                generation=payload.generation,
                parent_generation=payload.parent_generation,
                manifest_key=payload.manifest_key,
                manifest_sha256=payload.manifest_sha256,
                stored_bytes_added=payload.stored_bytes_added,
                final=payload.final,
            )
        )
        return DiskPublishResult(generation=generation)

    def release(self, payload: DiskReleasePayload, *, container: ContainerRecord) -> bool:
        self._authorize(payload.disk_id, container=container)
        return self.disks.release(
            payload.disk_id, container_id=container.id, lease_token=payload.lease_token
        )

    def _authorize(self, disk_id: str, *, container: ContainerRecord) -> None:
        identity = self.disks.identity(disk_id)
        if identity is None:
            raise NotFoundError(f"disk not found: {disk_id}")
        workspace_id, name = identity
        if workspace_id != container.workspace_id:
            raise AuthorizationDeniedError("disk does not belong to the container's workspace")
        if not container.stub_id:
            raise AuthorizationDeniedError("disk lease requires a container with a stub")
        with self.database.session() as session:
            stub = StubRepository(session).get(
                container.stub_id, workspace_id=container.workspace_id
            )
        if stub is None or all(mount.name != name for mount in stub.config.disks):
            raise AuthorizationDeniedError("container does not declare this disk")


__all__ = ["WorkerDiskLeaseService"]
