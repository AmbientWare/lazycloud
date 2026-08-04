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

from compute.offers import ComputeOffer
from compute.providers import ProviderPoolBootstrap
from database.context import ServiceContext
from networking.settings import TailnetControlSettings
from shared.compute_policy import ComputePoolRecord

# A launch template version already in service keeps working for this long after
# a refresh replaces it, so an instance that started from the previous version
# finishes booting. Comfortably longer than the sum of every bootstrap phase
# deadline.
POOL_BOOTSTRAP_SUPERSEDE_GRACE_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class PoolBootstrapProvisioner:
    """Everything a managed pool's nodes need at boot, in one place.

    Satisfies `compute.service.ProviderPoolBootstrapFactory`. The API and the
    scheduler both build one so the pinned artifacts and the control-plane
    origin cannot disagree between the process that creates a pool and the
    process that reconciles it.
    """

    control_plane_url: str
    agent_version: str
    agent_sha256: str
    agent_binary_url: str
    worker_image_digest: str

    def bootstrap(
        self,
        pool: ComputePoolRecord,
        offer: ComputeOffer,
    ) -> ProviderPoolBootstrap:
        del offer
        return ProviderPoolBootstrap(
            control_plane_url=self.control_plane_url,
            enrollment_request_id=pool.id,
            agent_version=self.agent_version,
            agent_sha256=self.agent_sha256,
            agent_binary_url=self.agent_binary_url,
            worker_image_digest=self.worker_image_digest,
        )



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
        control_plane_url=control_plane_url,
        agent_version=agent_version,
        agent_sha256=agent_sha256,
        agent_binary_url=agent_binary_url,
        worker_image_digest=worker_image_digest,
    )


__all__ = [
    "POOL_BOOTSTRAP_SUPERSEDE_GRACE_SECONDS",
    "PoolBootstrapProvisioner",
    "pool_bootstrap_provisioner",
]
