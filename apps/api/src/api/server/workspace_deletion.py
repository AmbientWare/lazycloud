from __future__ import annotations

import logging
from dataclasses import dataclass

from coordination.redis_client import REDIS_UNAVAILABLE_ERRORS, RedisClient
from database.repositories.compute import ComputeUnitRepository
from database.repositories.source_cache import SourceCacheCleanupRepository
from database.repositories.storage import ObjectRepository, VolumeRepository
from database.types import DatabaseSession
from gateway.service import GatewayControlService
from identity.workspaces import WorkspaceDeletionIdentityService
from observability.stream_state import RedisEventStreamRepository
from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.compute_policy import ComputeUnitPhase
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.http.volumes import DeleteVolumeRequest
from shared.identity import AuthTokenRecord, WorkspaceRecord, WorkspaceStatus
from shared.timestamps import utc_now

from api.server.services import ApiServices
from database import WorkspaceDeletionFence

logger = logging.getLogger(__name__)

_WORKSPACE_WORKLOAD_REDIS_ROOTS: tuple[tuple[str, ...], ...] = (
    ("endpoint",),
    ("function",),
    ("pod",),
    ("task",),
)


def _delete_workspace_workload_state(redis: RedisClient, workspace_id: str) -> int:
    keys: set[str] = set()
    for root in _WORKSPACE_WORKLOAD_REDIS_ROOTS:
        workspace_prefix = f"{redis.key(*root, workspace_id)}:"
        for key in redis.scan(redis.key(*root, "*")):
            if key.startswith(workspace_prefix):
                keys.add(key)
    return redis.delete(*sorted(keys)) if keys else 0


@dataclass(slots=True)
class WorkspaceDeletionService:
    services: ApiServices
    gateway: GatewayControlService

    def delete(
        self,
        workspace_id_or_name: str,
        *,
        audit_actor: AuthTokenRecord,
    ) -> WorkspaceRecord:
        identity = WorkspaceDeletionIdentityService(self.services.context)
        target = identity.resolve(workspace_id_or_name)
        self.gateway.require_workspace_self_hosted_decommissioned(target.id)
        with WorkspaceDeletionFence(self.services.database).acquire(target.id):
            workspace = self._begin(identity, target.id, audit_actor=audit_actor)
            if workspace.status is WorkspaceStatus.Deleted:
                return workspace

            self.services.auth.credentials_revoked()
            self._wake_source_cache_cleanup(workspace.id)
            self._delete_external_and_ephemeral_state(workspace)
            with self.services.database.session() as session:
                self._assert_storage_drained(session, workspace.id)
            try:
                self.services.workspace_storage_issuer.retire(
                    workspace_id=workspace.id, storage=workspace.storage
                )
            except Exception as exc:
                raise UpstreamUnavailableError(
                    "workspace storage retirement is incomplete"
                ) from exc
            if not workspace.storage.access_key and not workspace.storage.secret_key:
                with self.services.database.session() as session:
                    VolumeRepository(session).retire_cleanup(workspace.id)
            deleted = self._finalize(identity, workspace.id, audit_actor=audit_actor)
        return deleted

    def _begin(
        self,
        identity: WorkspaceDeletionIdentityService,
        workspace_id: str,
        *,
        audit_actor: AuthTokenRecord,
    ) -> WorkspaceRecord:
        with self.services.context.database.session() as session:
            workspace = identity.lock_and_validate_begin(
                session,
                workspace_id,
                actor_workspace_id=audit_actor.workspace_id,
            )
            if workspace.status is WorkspaceStatus.Deleted:
                return workspace
            self._assert_compute_released(session, workspace.id)
            source_object_ids = ObjectRepository(session).list_source_object_ids_for_deletion(
                workspace_id=workspace.id,
                source_bucket=SOURCE_PACKAGE_BUCKET,
            )
            SourceCacheCleanupRepository(session).add_targets(
                workspace_id=workspace.id,
                source_object_ids=source_object_ids,
                now=utc_now(),
            )
            return identity.mark_deleting(session, workspace)

    @staticmethod
    def _assert_compute_released(session: DatabaseSession, workspace_id: str) -> None:
        """Refuse while this workspace still holds capacity, but never mind the account.

        The connected AWS account belongs to the owner and backs their other
        workspaces, so requiring a disconnect here would make deleting a scratch
        workspace tear down production. What has to be released is the capacity this
        workspace itself holds.
        """
        pools = ComputeUnitRepository(session).list_internal(workspace_id=workspace_id)
        live = sorted(pool.name for pool in pools if pool.phase is not ComputeUnitPhase.Deleted)
        if live:
            # Deletion drains drained pools; it never terminates running capacity
            # on the tenant's behalf, so name the pools the operator must release.
            raise ConflictError(
                "release compute capacity before deleting this workspace; pools still active: "
                + ", ".join(live)
            )

    def _wake_source_cache_cleanup(self, workspace_id: str) -> None:
        try:
            self.services.worker_repository_service.wake_source_cache_cleanup(
                workspace_id=workspace_id
            )
        except REDIS_UNAVAILABLE_ERRORS:
            logger.warning(
                "source cache cleanup wake delivery failed; durable reconciliation remains pending"
            )

    def _delete_external_and_ephemeral_state(self, workspace: WorkspaceRecord) -> None:
        management = self.gateway.management
        container_targets = management.capture_container_shutdown_targets_for_workspace_deletion(
            workspace.id
        )
        containers = management.list_containers_for_workspace_deletion(workspace.id)
        container_ids = [container.id for container in containers]
        management.stop_all_active_deployments_for_workspace_deletion(workspace.id)
        management.stop_all_containers_for_workspace_deletion(workspace.id)
        self.services.container_shutdowns.confirm(container_targets)
        self.services.worker_repository_service.containers.delete_workspace_container_state(
            workspace.id,
            container_ids=container_ids,
        )
        _delete_workspace_workload_state(self.services.redis(), workspace.id)

        for pool in self.services.compute.list_pools_for_workspace_deletion(workspace.id):
            self.gateway.delete_pool_for_workspace_deletion(
                pool.name,
                workspace_id=workspace.id,
            )
        self.gateway.compute_states.delete_workspace_state(workspace.id)

        volumes_api = self.services.volume_service
        volumes = volumes_api.list_volumes_for_workspace_deletion(workspace.id)
        for volume in volumes.volumes:
            volumes_api.delete_volume_for_workspace_deletion(
                DeleteVolumeRequest(name=volume.name),
                workspace_id=workspace.id,
            )
        self.services.object_storage.delete_workspace_objects_for_deletion(workspace.id)

        self.services.map_service.delete_workspace(workspace.id)
        self.services.simple_queue_service.delete_workspace(workspace.id)
        self.services.signal_service.delete_workspace(workspace.id)
        self.services.workspace_changes.delete_workspace(workspace.id)
        RedisEventStreamRepository(self.services.redis_client).delete_workspace(workspace.id)

    def _finalize(
        self,
        identity: WorkspaceDeletionIdentityService,
        workspace_id: str,
        *,
        audit_actor: AuthTokenRecord,
    ) -> WorkspaceRecord:
        with self.services.context.database.session() as session:
            workspace = identity.lock_and_validate_begin(
                session,
                workspace_id,
                actor_workspace_id=audit_actor.workspace_id,
            )
            if workspace.status is WorkspaceStatus.Deleted:
                return workspace
            self._assert_storage_drained(session, workspace.id)
            return identity.finalize(session, workspace.id, actor=audit_actor)

    @staticmethod
    def _assert_storage_drained(session: DatabaseSession, workspace_id: str) -> None:
        objects = ObjectRepository(session)
        if objects.workspace_has_write_claims(workspace_id):
            raise ConflictError("workspace object writes are still active")
        if objects.workspace_has_objects(workspace_id):
            raise UpstreamUnavailableError("workspace object deletion is incomplete")
        if VolumeRepository(session).list(workspace_id=workspace_id):
            raise UpstreamUnavailableError("workspace volume deletion is incomplete")


__all__ = ["WorkspaceDeletionService"]
