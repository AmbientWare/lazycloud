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

from compute.capacity_errors import CapacityUnsatisfiableError
from compute.context import ComputeContext
from compute.offers import (
    ComputeOffer,
    OfferRequest,
    filter_offers,
    offer_disk_capacity_bytes,
    offer_matches_request,
    offer_selection_key,
)
from compute.providers import ResolvedComputeProvider

LOGGER = logging.getLogger(__name__)


class PooledCapacityOwner(Protocol):
    def prepared_capacity_owner_ids(self) -> frozenset[str]: ...
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


@dataclass(frozen=True, slots=True)
class ComputeCapacityPurchase:
    capacity_owner_id: str
    prepare: Callable[[], None]


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
            min_disk_bytes=requirements.disk_bytes,
            nodes=1,
        )
        shape_without_disks = purchase.model_copy(update={"min_disk_bytes": 0})
        offers: list[tuple[ResolvedComputeProvider, ComputeOffer]] = []
        disk_capacities: list[int] = []
        failures: list[str] = []
        for provider in providers:
            policy = provider.policy
            if provider.pooled is None or policy is None or not policy.can_purchase:
                continue
            try:
                machine_types = [
                    offer
                    for offer in provider.pooled.list_offers(root_volume_gib=policy.root_volume_gib)
                    if offer.provider == provider.ref
                    and offer.storage_mb >= policy.root_volume_gib * 1024
                    and (request.region is None or product_region(offer.region) is request.region)
                ]
                disk_capacities.extend(
                    offer_disk_capacity_bytes(offer)
                    for offer in machine_types
                    if offer_matches_request(offer, shape_without_disks)
                )
                candidates = filter_offers(
                    [
                        offer
                        for offer in machine_types
                        if self.compute.pooled_offer_rejection(
                            provider, offer, preemptible=requirements.preemptible
                        )
                        is None
                        and offer.cost_terms.complete_hourly_cost_micros is not None
                    ],
                    purchase,
                )
            except Exception:
                LOGGER.exception("provider offer discovery failed for %s", provider.ref)
                failures.append(provider.ref)
                continue
            offers.extend((provider, offer) for offer in candidates)
        purchases: dict[str, ComputeCapacityPurchase] = {}
        for provider, offer in sorted(
            offers, key=lambda item: offer_selection_key(item[1], purchase)
        ):
            owner_id = self.compute.pooled_offer_owner_id(provider, offer)
            purchases.setdefault(
                owner_id,
                ComputeCapacityPurchase(
                    capacity_owner_id=owner_id,
                    prepare=partial(self._prepare_offer, provider, offer, requirements),
                ),
            )
        if not purchases and not failures and disk_capacities:
            largest = max(disk_capacities)
            if largest < requirements.disk_bytes:
                raise CapacityUnsatisfiableError(
                    f"durable disks declare {requirements.disk_bytes} bytes, more than the "
                    f"{largest} bytes any machine type in placement {request.placement.key} "
                    "can hold",
                    code="disk_capacity_exceeded",
                )
        if not purchases:
            detail = (
                f"; unavailable providers: {', '.join(sorted(set(failures)))}" if failures else ""
            )
            raise UpstreamUnavailableError(
                f"no provider capacity meets the workload requirements and purchase policy{detail}",
                code="offer_unavailable",
            )
        prepared = self.compute.prepared_capacity_owner_ids()
        return tuple(
            sorted(purchases.values(), key=lambda item: item.capacity_owner_id not in prepared)
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
