from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from database.repositories.apps import DeploymentRepository
from shared.compute_policy import ComputeResourceRequirements, ComputeUnitRecord, MachinePool
from shared.contracts import ContractModel
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.placement import ProductRegion, product_region

from compute.context import ComputeContext
from compute.offers import ComputeOffer, OfferRequest, filter_offers, offer_selection_key
from compute.policy import WorkspaceComputePolicyService
from compute.providers import ResolvedComputeProvider

LOGGER = logging.getLogger(__name__)


class PooledCapacityOwner(Protocol):
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
    requested_pool: str = ""
    region: ProductRegion | None = None
    requirements: ComputeResourceRequirements


@dataclass(frozen=True, slots=True)
class ComputeCapacityPlacementResult:
    pool: MachinePool


@dataclass(frozen=True, slots=True)
class ComputeCapacityPurchase:
    capacity_owner_id: str
    prepare: Callable[[], None]


@dataclass(slots=True)
class ComputeCapacityPlacementService:
    context: ComputeContext
    policies: WorkspaceComputePolicyService
    compute: PooledCapacityOwner

    def place(self, request: ComputeCapacityPlacementRequest) -> ComputeCapacityPlacementResult:
        return ComputeCapacityPlacementResult(pool=MachinePool(self._machine_pool_for(request)))

    def purchase_candidates(
        self, request: ComputeCapacityPlacementRequest
    ) -> tuple[ComputeCapacityPurchase, ...]:
        """Return approved purchase candidates in GPU-preference and cost order."""
        pool = self._machine_pool_for(request)
        providers = tuple(
            provider
            for provider in self.compute.pooled_providers(request.workspace_id)
            if provider.policy is not None and provider.policy.pool == pool
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
            if provider.pooled is None or policy is None:
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
        if not purchases:
            detail = (
                f"; unavailable providers: {', '.join(sorted(set(failures)))}" if failures else ""
            )
            raise UpstreamUnavailableError(
                f"no provider capacity meets the workload requirements and purchase policy{detail}",
                code="offer_unavailable",
            )
        return tuple(purchases.values())

    def _prepare_offer(
        self,
        provider: ResolvedComputeProvider,
        offer: ComputeOffer,
        requirements: ComputeResourceRequirements,
    ) -> None:
        self.compute.prepare_pooled_offer(provider=provider, offer=offer, requirements=requirements)

    def _machine_pool_for(self, request: ComputeCapacityPlacementRequest) -> str:
        if request.requested_pool:
            return request.requested_pool
        deployment_pool = self._deployment_machine_pool(request)
        if deployment_pool:
            return deployment_pool
        return self.policies.default_machine_pool(workspace=request.workspace_id)

    def _deployment_machine_pool(self, request: ComputeCapacityPlacementRequest) -> str:
        """The group a deployment was pinned to when it was created.

        A deployment keeps the fleet it was deployed onto: a workspace that
        later changes its default must not move workloads already running.
        """
        if not request.deployment_id:
            return ""
        with self.context.database.session() as session:
            deployment = DeploymentRepository(session).get(
                request.deployment_id,
                workspace_id=request.workspace_id,
            )
        if deployment is None:
            raise InvalidInputError(
                f"deployment {request.deployment_id!r} was not found in the workspace"
            )
        return deployment.pool


__all__ = [
    "ComputeCapacityPlacementRequest",
    "ComputeCapacityPlacementResult",
    "ComputeCapacityPlacementService",
    "ComputeCapacityPurchase",
]
