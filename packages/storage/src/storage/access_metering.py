from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import batched
from uuid import UUID

from database.repositories.identity import WorkspaceRepository
from database.repositories.storage_access import StorageAccessRepository
from shared.storage_access import StorageAccessObservation, StorageAccessSource
from storage_client.s3 import S3ObjectStoreSettings

from database import DatabaseClient


@dataclass(slots=True)
class StorageAccessMeteringService:
    database: DatabaseClient
    source: StorageAccessSource
    settings: S3ObjectStoreSettings

    def reconcile(self) -> int:
        delivery = self.source.receive()
        if delivery is None:
            return 0
        inserted = 0
        for key in delivery.keys:
            for observations in batched(self.source.read(key), 250):
                inserted += record_storage_access(self.database, self.settings, observations)
                self.source.renew(delivery)
        self.source.acknowledge(delivery)
        return inserted


def record_storage_access(
    database: DatabaseClient,
    settings: S3ObjectStoreSettings,
    observations: Sequence[StorageAccessObservation],
) -> int:
    inserted = 0
    with database.session() as session:
        repository = StorageAccessRepository(session)
        for observation in observations:
            workspace_id = _workspace_id(observation, settings)
            if workspace_id is not None:
                workspace = WorkspaceRepository(session).get(workspace_id)
                if workspace is None or (
                    observation.bucket != settings.bucket
                    and (
                        workspace.storage.access_key
                        or workspace.storage.secret_key
                        or workspace.storage.bucket != observation.bucket
                        or workspace.storage.endpoint_url != settings.endpoint_url
                    )
                ):
                    workspace_id = None
            inserted += repository.append(observation, workspace_id=workspace_id)
    return inserted


def _workspace_id(
    observation: StorageAccessObservation, settings: S3ObjectStoreSettings
) -> str | None:
    candidate = ""
    if observation.bucket == settings.bucket:
        parts = observation.key.split("/", 2)
        if len(parts) == 3 and parts[0] == "workspaces":
            candidate = parts[1]
    elif observation.bucket.startswith(f"{settings.workspace_bucket_prefix}-"):
        candidate = observation.bucket.removeprefix(f"{settings.workspace_bucket_prefix}-")
    try:
        return str(UUID(candidate))
    except ValueError:
        return None
