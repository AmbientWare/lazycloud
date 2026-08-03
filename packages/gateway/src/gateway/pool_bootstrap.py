"""The tailnet key a managed pool's launch template hands every node it starts.

A node has to reach the control plane before it has an agent or an identity,
and it now does that over the tailnet rather than a public origin. Since one
autoscaling group launches N instances from one launch template, the key in that
template cannot be bound to a machine that does not exist yet: it is reusable,
and the tag is what keeps it narrow. The node trades it for a single-use,
machine-scoped key the moment enrolment gives it an identity.

The key is durable because the launch template outlives any single launch — an
autoscaling group that scales up days later boots from the same user-data. It is
refreshed rather than reissued per reconcile, because every change to user-data
forces a new launch-template version.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from compute.offers import ComputeOffer
from compute.providers import ProviderPoolBootstrap
from database.context import ServiceContext
from database.repositories.compute import PoolBootstrapCredentialRepository
from database.tailnet_cleanup import DatabaseTailnetCleanupStore
from networking.settings import TailnetControlSettings
from networking.tailnet_cleanup import TailnetCleanupCoordinator
from networking.tailnet_control import (
    DEFAULT_POOL_BOOTSTRAP_KEY_REFRESH_SECONDS,
    TailnetControl,
    TailscaleTailnetControl,
)
from pydantic import SecretStr
from shared.compute_enrollment import PoolBootstrapCredential
from shared.compute_policy import ComputePoolRecord

# A launch template version already in service keeps working for this long after
# a refresh replaces it, so an instance that started from the previous version
# finishes booting. Comfortably longer than the sum of every bootstrap phase
# deadline.
POOL_BOOTSTRAP_SUPERSEDE_GRACE_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class PoolBootstrapCredentialService:
    context: ServiceContext
    control: TailnetControl
    tag: str
    refresh_seconds: int = DEFAULT_POOL_BOOTSTRAP_KEY_REFRESH_SECONDS
    supersede_grace_seconds: int = POOL_BOOTSTRAP_SUPERSEDE_GRACE_SECONDS

    def credential(self, pool: ComputePoolRecord, *, mint: bool) -> SecretStr:
        """The key this pool's user-data should carry.

        Returns the stored key while it has more than `refresh_seconds` left.
        With `mint` false — pool deletion, and the reads that only need a spec
        to look something up — a pool with no stored key gets none rather than
        one minted on its way out.
        """
        now = datetime.now(UTC)
        with self.context.database.session() as session:
            credentials = PoolBootstrapCredentialRepository(session)
            existing = credentials.get(pool.id, for_update=True)
            fresh_enough = existing is not None and existing.expires_at - now > timedelta(
                seconds=self.refresh_seconds
            )
            if fresh_enough or not mint:
                return SecretStr(existing.auth_key) if existing is not None else SecretStr("")

            issued = self.control.issue_pool_bootstrap_key(pool_id=pool.id)
            superseded = list(existing.superseded_auth_key_ids) if existing is not None else []
            if existing is not None:
                superseded.append(existing.auth_key_id)
            credentials.save(
                PoolBootstrapCredential(
                    id=existing.id if existing is not None else str(uuid4()),
                    workspace_id=pool.workspace_id,
                    pool_id=pool.id,
                    pool_name=pool.name,
                    tag=self.tag,
                    auth_key_id=issued.id,
                    auth_key=issued.key.get_secret_value(),
                    expires_at=issued.expires_at,
                    superseded_auth_key_ids=superseded,
                    created_at=existing.created_at if existing is not None else now,
                    updated_at=now,
                )
            )

        if existing is not None:
            self._revoke(
                pool,
                auth_key_ids=(existing.auth_key_id,),
                # Grace, not immediacy: instances launched from the template
                # version this one replaced are still redeeming the old key.
                not_before=now + timedelta(seconds=self.supersede_grace_seconds),
            )
        return issued.key

    def release(self, pool: ComputePoolRecord) -> None:
        """Revoke a deleted pool's key and forget it.

        No grace period: the launch template is going away with the pool, so a
        node still booting from it has nothing left to enrol into.
        """
        with self.context.database.session() as session:
            removed = PoolBootstrapCredentialRepository(session).delete(pool.id)
        if removed is None:
            return
        self._revoke(
            pool,
            auth_key_ids=(removed.auth_key_id, *removed.superseded_auth_key_ids),
            not_before=None,
        )

    def _revoke(
        self,
        pool: ComputePoolRecord,
        *,
        auth_key_ids: tuple[str, ...],
        not_before: datetime | None,
    ) -> None:
        keys = tuple(key_id for key_id in auth_key_ids if key_id)
        if not keys:
            return
        # Reuse the durable, retrying cleanup path rather than a best-effort
        # call here: a revoke that fails while Tailscale is unavailable must
        # still happen, and this store already owns that guarantee. The pool's
        # capacity owner stands in for a machine id so the pool gets its own
        # tombstone row.
        TailnetCleanupCoordinator(
            DatabaseTailnetCleanupStore(self.context),
            self.control,
        ).defer_machine_cleanup(
            workspace_id=pool.workspace_id,
            pool_name=pool.name,
            machine_id=pool.capacity_owner_id,
            generations=(),
            auth_key_ids=keys,
            device_ids=(),
            auth_key_expires_at=not_before,
        )


@dataclass(frozen=True, slots=True)
class PoolBootstrapProvisioner:
    """Everything a managed pool's nodes need at boot, in one place.

    Satisfies `compute.service.ProviderPoolBootstrapFactory`. The API and the
    scheduler both build one so the pinned artifacts and the control-plane
    origin cannot disagree between the process that creates a pool and the
    process that reconciles it.
    """

    credentials: PoolBootstrapCredentialService
    control_plane_url: str
    agent_version: str
    agent_sha256: str
    agent_binary_url: str
    worker_image_digest: str

    def bootstrap(
        self,
        pool: ComputePoolRecord,
        offer: ComputeOffer,
        *,
        writes_launch_template: bool,
    ) -> ProviderPoolBootstrap:
        del offer
        return ProviderPoolBootstrap(
            control_plane_url=self.control_plane_url,
            enrollment_request_id=pool.id,
            agent_version=self.agent_version,
            agent_sha256=self.agent_sha256,
            agent_binary_url=self.agent_binary_url,
            worker_image_digest=self.worker_image_digest,
            tailnet_auth_key=self.credentials.credential(pool, mint=writes_launch_template),
        )

    def release(self, pool: ComputePoolRecord) -> None:
        self.credentials.release(pool)


def pool_bootstrap_provisioner(
    context: ServiceContext,
    *,
    control_plane_url: str,
    agent_version: str,
    agent_sha256: str,
    agent_binary_url: str,
    worker_image_digest: str,
    tailnet_control: TailnetControlSettings,
) -> PoolBootstrapProvisioner:
    """Build the provisioner both the API and the scheduler compose.

    Shared rather than duplicated per app because the two processes reconcile
    the same pools: a disagreement about the control-plane origin or the pinned
    artifacts would show up as launch templates that alternate between two
    versions.
    """
    return PoolBootstrapProvisioner(
        credentials=PoolBootstrapCredentialService(
            context=context,
            control=TailscaleTailnetControl(tailnet_control.to_control_config()),
            tag=tailnet_control.pool_bootstrap_tag,
            refresh_seconds=tailnet_control.pool_bootstrap_key_refresh_seconds,
        ),
        control_plane_url=control_plane_url,
        agent_version=agent_version,
        agent_sha256=agent_sha256,
        agent_binary_url=agent_binary_url,
        worker_image_digest=worker_image_digest,
    )


__all__ = [
    "POOL_BOOTSTRAP_SUPERSEDE_GRACE_SECONDS",
    "PoolBootstrapCredentialService",
    "PoolBootstrapProvisioner",
    "pool_bootstrap_provisioner",
]
