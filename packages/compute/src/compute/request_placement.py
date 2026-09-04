from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from database.repositories.apps import DeploymentRepository
from database.repositories.compute import ComputeUnitRepository
from shared.compute_policy import ComputeResourceRequirements, ComputeUnitRecord, MachinePool
from shared.container_requests import capacity_with_overhead
from shared.contracts import ContractModel
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.gpu import gpu_preference_accepts
from shared.placement import ProductRegion, product_region

from compute.context import ComputeContext
from compute.offers import OfferRequest, choose_offer
from compute.policy import WorkspaceComputePolicyService
from compute.providers import ResolvedComputeProvider


class PooledCapacityOwner(Protocol):
    def pooled_providers(self, workspace_id: str) -> tuple[ResolvedComputeProvider, ...]: ...

    def prepare_pooled_capacity(
        self,
        *,
        workspace: str,
        requirements: ComputeResourceRequirements,
        region: str,
        desired_machines: int,
        root_volume_gib: int,
        idle_timeout_seconds: int = 300,
        allowed_instance_types: tuple[str, ...] = (),
        provider_ref: str = "",
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


@dataclass(slots=True)
class ComputeCapacityPlacementService:
    context: ComputeContext
    policies: WorkspaceComputePolicyService
    compute: PooledCapacityOwner

    def place(self, request: ComputeCapacityPlacementRequest) -> ComputeCapacityPlacementResult:
        """Resolve compatible capacity inside the requested pool and region."""
        pool = self._machine_pool_for(request)
        providers = tuple(
            provider
            for provider in self.compute.pooled_providers(request.workspace_id)
            if provider.policy is not None and provider.policy.pool == pool
        )
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, request.workspace_id).id
            capacity_workspaces = {
                workspace_id,
                *(provider.policy.workspace_id for provider in providers if provider.policy),
            }
            repository = ComputeUnitRepository(session)
            units = [
                unit
                for capacity_workspace in capacity_workspaces
                for unit in repository.list_for_machine_pool(capacity_workspace, MachinePool(pool))
                if capacity_workspace == workspace_id or unit.platform_fleet
            ]
        # Paid capacity can keep serving work during a supplier API outage.
        # Any new purchase still validates the live offer in the capacity owner.
        if any(
            _pool_supports(unit, request.requirements)
            and (request.region is None or product_region(unit.region) is request.region)
            and (not unit.provider_ref or unit.observed_machines > 0)
            for unit in units
        ):
            return ComputeCapacityPlacementResult(pool=MachinePool(pool))
        if not providers:
            return ComputeCapacityPlacementResult(pool=MachinePool(pool))
        offers = [
            offer
            for provider in providers
            if provider.pooled is not None and provider.policy is not None
            for offer in provider.pooled.list_offers()
            if provider.policy.accepts(offer)
            and offer.storage_mb >= provider.policy.root_volume_gib * 1024
            and (request.region is None or product_region(offer.region) is request.region)
        ]
        offered = {(offer.provider, offer.id, offer.region) for offer in offers}
        if any(
            _pool_supports(unit, request.requirements)
            and (request.region is None or product_region(unit.region) is request.region)
            and (
                not unit.provider_ref or (unit.provider_ref, unit.offer_id, unit.region) in offered
            )
            for unit in units
        ):
            return ComputeCapacityPlacementResult(pool=MachinePool(pool))
        requirements = request.requirements
        try:
            offer = choose_offer(
                offers,
                OfferRequest(
                    min_cpu_millicores=requirements.cpu_millicores,
                    min_memory_mb=requirements.memory_mb,
                    architecture=requirements.architecture or "amd64",
                    runtime=requirements.runtime,
                    gpu=requirements.gpu,
                    min_gpu_count=requirements.gpu_count,
                    nodes=1,
                ),
            )
        except ValueError as exc:
            raise UpstreamUnavailableError(
                "no provider capacity meets the workload and region requirements",
                code="offer_unavailable",
            ) from exc
        selected = next(provider for provider in providers if provider.ref == offer.provider)
        configuration = selected.policy
        assert configuration is not None
        self.compute.prepare_pooled_capacity(
            workspace=request.workspace_id,
            requirements=request.requirements,
            region=offer.region,
            desired_machines=0,
            root_volume_gib=configuration.root_volume_gib,
            idle_timeout_seconds=configuration.idle_timeout_seconds,
            allowed_instance_types=configuration.allowed_instance_types,
            provider_ref=selected.ref,
        )
        return ComputeCapacityPlacementResult(pool=MachinePool(pool))

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


def _pool_supports(pool: ComputeUnitRecord, requirements: ComputeResourceRequirements) -> bool:
    # The same overhead that offer selection applies, so a pool judged able to host a
    # shape is one that would have been chosen for it. Judging an existing pool
    # by the raw request while sizing a new one with headroom would place work on
    # nodes that were never big enough for it.
    if pool.worker_cpu_millicores < capacity_with_overhead(requirements.cpu_millicores):
        return False
    if pool.worker_memory_mib < capacity_with_overhead(requirements.memory_mb):
        return False
    if requirements.runtime not in pool.worker_runtimes:
        return False
    if requirements.gpu_count == 0:
        return pool.worker_gpu_count == 0
    if pool.worker_gpu_count < requirements.gpu_count:
        return False
    return gpu_preference_accepts(requirements.gpu, pool.worker_gpu_type)


__all__ = [
    "ComputeCapacityPlacementRequest",
    "ComputeCapacityPlacementResult",
    "ComputeCapacityPlacementService",
]
