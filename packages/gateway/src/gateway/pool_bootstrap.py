"""What a managed pool's launch template tells every node it starts."""

from __future__ import annotations

from dataclasses import dataclass

from compute.offers import ComputeOffer
from compute.providers import ProviderUnitBootstrap
from shared.compute_policy import ComputeUnitRecord

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

    def bootstrap(
        self,
        pool: ComputeUnitRecord,
        offer: ComputeOffer,
    ) -> ProviderUnitBootstrap:
        del offer
        return ProviderUnitBootstrap(
            control_plane_url=self.control_plane_url,
            enrollment_request_id=pool.id,
            agent_version=self.agent_version,
            agent_sha256=self.agent_sha256,
            agent_binary_url=self.agent_binary_url,
        )


def pool_bootstrap_provisioner(
    *,
    control_plane_url: str,
    agent_version: str,
    agent_sha256: str,
    agent_binary_url: str,
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
    )


__all__ = [
    "POOL_BOOTSTRAP_SUPERSEDE_GRACE_SECONDS",
    "PoolBootstrapProvisioner",
    "pool_bootstrap_provisioner",
]
