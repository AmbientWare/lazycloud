"""What a managed pool's launch template tells every node it starts."""

from __future__ import annotations

from dataclasses import dataclass

from compute.offers import ComputeOffer
from compute.providers import ProviderUnitBootstrap
from shared.compute_policy import ComputeUnitRecord


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


__all__ = [
    "PoolBootstrapProvisioner",
]
