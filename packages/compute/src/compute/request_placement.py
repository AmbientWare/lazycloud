from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from shared.compute_policy import ComputeResourceRequirements, ComputeUnitRecord
from shared.contracts import ContractModel
from shared.errors import UpstreamUnavailableError
from shared.placement import Placement, ProductRegion, product_region

from compute.context import ComputeContext
from compute.fleet_reserves import ReserveAdmission
from compute.offers import ComputeOffer, OfferRequest, filter_offers, offer_selection_key
from compute.providers import ResolvedComputeProvider

LOGGER = logging.getLogger(__name__)


class PooledCapacityOwner(Protocol):
    def reserve_admission(self) -> ReserveAdmission: ...
    def pooled_providers(self, workspace_id: str) -> tuple[ResolvedComputeProvider, ...]: ...

    def pooled_offer_rejection(
        self,
        provider: ResolvedComputeProvider,
        offer: ComputeOffer,
        *,
        preemptible: bool,
    ) -> str | None: ...

    def pooled_offer_owner_id(
        self, provider: ResolvedComputeProvider, offer: ComputeOffer
    ) -> str: ...

    def prepare_pooled_offer(
        self,
        *,
        provider: ResolvedComputeProvider,
        offer: ComputeOffer,
        requirements: ComputeResourceRequirements,
    ) -> ComputeUnitRecord: ...


class ComputeCapacityPlacementRequest(ContractModel):
    workspace_id: str
    deployment_id: str = ""
    placement: Placement
    """Where the workload's stub was pinned when it was created."""
    region: ProductRegion | None = None
    requirements: ComputeResourceRequirements
    preferred_availability_zone: str = ""
    """The zone of a volume the workload's disk left cached, where it attaches without a restore."""


@dataclass(frozen=True, slots=True)
class ComputeCapacityPurchase:
    capacity_owner_id: str
    prepare: Callable[[], None]
    availability_zone: str = ""


@dataclass(slots=True)
class ComputeCapacityPlacementService:
    context: ComputeContext
    compute: PooledCapacityOwner

    def purchase_candidates(
        self, request: ComputeCapacityPlacementRequest
    ) -> tuple[ComputeCapacityPurchase, ...]:
        """Return approved purchase candidates in GPU-preference and cost order."""
        providers = tuple(
            provider
            for provider in self.compute.pooled_providers(request.workspace_id)
            if provider.policy is not None and provider.policy.placement == request.placement
        )
        requirements = request.requirements
        purchase = OfferRequest(
            min_cpu_millicores=requirements.cpu_millicores,
            min_memory_mb=requirements.memory_mb,
            architecture=requirements.architecture or "amd64",
            preemptible=requirements.preemptible,
            availability_zone=requirements.availability_zone,
            runtime=requirements.runtime,
            gpu=requirements.gpu,
            min_gpu_count=requirements.gpu_count,
            nodes=1,
        )
        offers: list[tuple[ResolvedComputeProvider, ComputeOffer]] = []
        failures: list[str] = []
        for provider in providers:
            policy = provider.policy
            if provider.pooled is None or policy is None or not policy.can_purchase:
                continue
            try:
                candidates = filter_offers(
                    [
                        offer
                        for offer in provider.pooled.list_offers(
                            root_volume_gib=policy.root_volume_gib
                        )
                        if offer.provider == provider.ref
                        and self.compute.pooled_offer_rejection(
                            provider, offer, preemptible=requirements.preemptible
                        )
                        is None
                        and offer.storage_mb >= policy.root_volume_gib * 1024
                        and (
                            request.region is None or product_region(offer.region) is request.region
                        )
                        and offer.cost_terms.complete_hourly_cost_micros is not None
                    ],
                    purchase,
                )
            except Exception:
                LOGGER.exception("provider offer discovery failed for %s", provider.ref)
                failures.append(provider.ref)
                continue
            offers.extend((provider, offer) for offer in candidates)
        admission = self.compute.reserve_admission()
        purchases: dict[str, ComputeCapacityPurchase] = {}
        for provider, offer in sorted(
            offers, key=lambda item: offer_selection_key(item[1], purchase)
        ):
            owner_id = self.compute.pooled_offer_owner_id(provider, offer)
            if requirements.preemptible and owner_id in admission.withheld_from_preemptible:
                continue
            purchases.setdefault(
                owner_id,
                ComputeCapacityPurchase(
                    capacity_owner_id=owner_id,
                    prepare=partial(self._prepare_offer, provider, offer, requirements),
                    availability_zone=offer.availability_zone,
                ),
            )
        if not purchases:
            detail = (
                f"; unavailable providers: {', '.join(sorted(set(failures)))}" if failures else ""
            )
            raise UpstreamUnavailableError(
                f"no provider capacity meets the workload requirements and purchase policy{detail}",
                code="offer_unavailable",
            )
        # A stopped reserve starts in seconds and a purchase takes a minute, so any
        # prepared pool comes first; within each, the disk's cached volume zone.
        prepared = admission.prepared
        preferred = request.preferred_availability_zone
        return tuple(
            sorted(
                purchases.values(),
                key=lambda item: (
                    item.capacity_owner_id not in prepared,
                    bool(preferred) and item.availability_zone != preferred,
                ),
            )
        )

    def _prepare_offer(
        self,
        provider: ResolvedComputeProvider,
        offer: ComputeOffer,
        requirements: ComputeResourceRequirements,
    ) -> None:
        self.compute.prepare_pooled_offer(provider=provider, offer=offer, requirements=requirements)


__all__ = [
    "ComputeCapacityPlacementRequest",
    "ComputeCapacityPlacementService",
    "ComputeCapacityPurchase",
]
