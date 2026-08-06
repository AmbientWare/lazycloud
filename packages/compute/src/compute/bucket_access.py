from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from database.repositories.apps import DeploymentRepository
from database.repositories.compute import AwsAccountConnectionRepository
from pydantic import BaseModel, ConfigDict
from shared.aws_connections import AwsAccountConnection, AwsAccountConnectionPhase
from shared.deployment_records import Deployment, DeploymentSpec
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.mounts import MountAuthMode, normalize_mount_prefix
from shared.timestamps import utc_now

from compute.context import ComputeContext


@dataclass(frozen=True, slots=True)
class ConnectedBucketAccessGrant:
    bucket: str
    prefix: str
    read_only: bool


class AwsNodeBucketAccessController(Protocol):
    def reconcile(
        self,
        connection: AwsAccountConnection,
        grants: tuple[ConnectedBucketAccessGrant, ...],
    ) -> None: ...


class AwsConnectionBucketAccessReconciler(Protocol):
    def reconcile_connection(self, connection: AwsAccountConnection) -> None: ...


class _CloudBucketMountConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bucket_name: str = ""
    prefix: str = ""
    auth_mode: MountAuthMode = MountAuthMode.Ambient
    endpoint_url: str = ""
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class AwsDeploymentBucketAccessService:
    context: ComputeContext
    controller: AwsNodeBucketAccessController

    def reconcile_deployments(
        self,
        *,
        workspace: str,
        required: bool = True,
    ) -> None:
        workspace_id, connection, grants = self._snapshot(workspace)
        if connection is None or not connection.can_manage_existing_capacity:
            if grants and required:
                raise InvalidInputError(
                    "connected AWS bucket mounts require an active AWS account connection"
                )
            return
        self._mark_pending(connection.id)
        try:
            self.controller.reconcile(connection, grants)
        except (ValueError, RuntimeError) as exc:
            self._mark_pending(connection.id)
            if required:
                raise UpstreamUnavailableError(
                    "connected AWS bucket access could not be reconciled"
                ) from exc
            return
        self._complete_if_current(
            connection.id,
            workspace_id=workspace_id,
            applied_digest=_grants_digest(grants),
        )

    def reconcile_connection(self, connection: AwsAccountConnection) -> None:
        _, current, grants = self._snapshot(connection.workspace_id)
        if current is None or current.id != connection.id:
            return
        try:
            self.controller.reconcile(connection, grants)
        except (ValueError, RuntimeError) as exc:
            raise UpstreamUnavailableError(
                "connected AWS bucket access could not be reconciled"
            ) from exc

    def _snapshot(
        self,
        workspace: str,
    ) -> tuple[
        str,
        AwsAccountConnection | None,
        tuple[ConnectedBucketAccessGrant, ...],
    ]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            connection = AwsAccountConnectionRepository(session).get_for_workspace(workspace_id)
            deployments = DeploymentRepository(session).list(
                workspace_id=workspace_id,
                active=True,
            )
        grants = _deployment_bucket_access_grants(
            _deployments_on_connection(deployments, connection)
        )
        return workspace_id, connection, grants

    def _mark_pending(self, connection_id: str) -> None:
        now = utc_now()
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            current = repository.get(connection_id, for_update=True)
            if current is None:
                return
            repository.save(
                current.model_copy(
                    update={
                        "revision": current.revision + 1,
                        "bucket_access_reconcile_pending": True,
                        "next_reconcile_at": now,
                        "updated_at": now,
                    }
                )
            )

    def _complete_if_current(
        self,
        connection_id: str,
        *,
        workspace_id: str,
        applied_digest: str,
    ) -> None:
        now = utc_now()
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            current = repository.get(connection_id, for_update=True)
            if current is None:
                return
            deployments = DeploymentRepository(session).list(
                workspace_id=workspace_id,
                active=True,
            )
            current_grants = _deployment_bucket_access_grants(
                _deployments_on_connection(deployments, repository.get(connection_id))
            )
            still_current = _grants_digest(current_grants) == applied_digest
            repository.save(
                current.model_copy(
                    update={
                        "revision": current.revision + 1,
                        "bucket_access_reconcile_pending": not still_current,
                        "next_reconcile_at": (
                            None
                            if still_current and current.phase is AwsAccountConnectionPhase.Ready
                            else current.next_reconcile_at or now
                        ),
                        "updated_at": now,
                    }
                )
            )


def _deployments_on_connection(
    deployments: Sequence[Deployment],
    connection: AwsAccountConnection | None,
) -> tuple[Deployment, ...]:
    """Deployments whose pool this connection provisions into.

    The pool is what ties a workload to an account now: a deployment running
    in the pool a connection stamps on its units needs that account's bucket
    grants, and one in any other pool does not.
    """
    if connection is None:
        return ()
    return tuple(deployment for deployment in deployments if deployment.pool == connection.pool)


def _deployment_bucket_access_grants(
    deployments: tuple[Deployment, ...],
) -> tuple[ConnectedBucketAccessGrant, ...]:
    grants: dict[tuple[str, str], bool] = {}
    for deployment in deployments:
        for grant in _spec_bucket_access_grants(deployment.spec):
            key = (grant.bucket, grant.prefix)
            grants[key] = grants.get(key, True) and grant.read_only
    return tuple(
        ConnectedBucketAccessGrant(bucket=bucket, prefix=prefix, read_only=read_only)
        for (bucket, prefix), read_only in sorted(grants.items())
    )


def _grants_digest(grants: tuple[ConnectedBucketAccessGrant, ...]) -> str:
    payload: list[dict[str, str | bool]] = [
        {
            "bucket": grant.bucket,
            "prefix": grant.prefix,
            "read_only": grant.read_only,
        }
        for grant in grants
    ]
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _spec_bucket_access_grants(spec: DeploymentSpec) -> tuple[ConnectedBucketAccessGrant, ...]:
    grants: list[ConnectedBucketAccessGrant] = []
    for mount in spec.volumes:
        if mount.config is None:
            continue
        config = _CloudBucketMountConfig.model_validate(mount.config)
        if not config.bucket_name or config.auth_mode is not MountAuthMode.Ambient:
            continue
        if config.endpoint_url:
            raise InvalidInputError(
                "ambient connected AWS bucket mounts cannot use a custom S3 endpoint"
            )
        grants.append(
            ConnectedBucketAccessGrant(
                bucket=config.bucket_name,
                prefix=normalize_mount_prefix(config.prefix).rstrip("/"),
                read_only=mount.read_only or config.read_only,
            )
        )
    return tuple(grants)


__all__ = [
    "AwsConnectionBucketAccessReconciler",
    "AwsDeploymentBucketAccessService",
    "AwsNodeBucketAccessController",
    "ConnectedBucketAccessGrant",
]
