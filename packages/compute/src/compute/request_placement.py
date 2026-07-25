from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from database.repositories.apps import DeploymentRepository
from database.repositories.orchestration import PoolRepository
from shared.capacity import CapacityOwnerKind
from shared.compute_fleet import Pool
from shared.compute_policy import (
    ComputePlacement,
    ComputePlacementTarget,
    ComputePoolRecord,
    ComputeResourceRequirements,
)
from shared.contracts import ContractModel
from shared.errors import InvalidInputError
from shared.gpu import GPU_ANY, normalize_gpu_type

from compute.context import ComputeContext
from compute.policy import WorkspaceComputePolicyService


class PooledCapacityOwner(Protocol):
    def prepare_pooled_capacity(
        self,
        *,
        workspace: str,
        requirements: ComputeResourceRequirements,
        region: str,
        desired_machines: int,
        workspace_machine_limit: int,
        root_volume_gib: int,
        idle_timeout_seconds: int = 300,
        allowed_instance_types: tuple[str, ...] = (),
    ) -> ComputePoolRecord: ...


class ComputeCapacityPlacementRequest(ContractModel):
    workspace_id: str
    deployment_id: str = ""
    attached_pool: str = ""
    requested_placement: ComputePlacementTarget | None = None
    requirements: ComputeResourceRequirements


@dataclass(frozen=True, slots=True)
class ComputeCapacityPlacementResult:
    placement: ComputePlacement
    capacity_owner_id: str | None = None


@dataclass(slots=True)
class ComputeCapacityPlacementService:
    context: ComputeContext
    policies: WorkspaceComputePolicyService
    compute: PooledCapacityOwner

    def place(self, request: ComputeCapacityPlacementRequest) -> ComputeCapacityPlacementResult:
        placement = self._deployment_placement(request)
        if placement is None:
            placement = self.policies.resolve_placement(
                workspace=request.workspace_id,
                requested=request.requested_placement,
                attached_pool=request.attached_pool,
                requirements=request.requirements,
            )
        selected_pool = self._scheduler_pool(request, placement)
        if selected_pool is not None:
            return ComputeCapacityPlacementResult(
                placement=placement.model_copy(update={"pool_name": selected_pool.name}),
                capacity_owner_id=selected_pool.capacity_owner_id,
            )
        if placement.target is not ComputePlacementTarget.Aws:
            return ComputeCapacityPlacementResult(placement=placement)

        policy = self.policies.get_policy(workspace=request.workspace_id)
        aws = policy.aws
        machine_limit = (
            aws.max_gpu_instances if request.requirements.gpu_count > 0 else aws.max_cpu_instances
        )
        pool = self.compute.prepare_pooled_capacity(
            workspace=request.workspace_id,
            requirements=request.requirements,
            region=placement.region,
            desired_machines=0,
            workspace_machine_limit=machine_limit,
            root_volume_gib=aws.root_volume_gib,
            idle_timeout_seconds=aws.idle_timeout_seconds,
            allowed_instance_types=aws.allowed_instance_types,
        )
        return ComputeCapacityPlacementResult(
            placement=placement.model_copy(update={"pool_name": pool.name}),
            capacity_owner_id=pool.capacity_owner_id,
        )

    def _deployment_placement(
        self,
        request: ComputeCapacityPlacementRequest,
    ) -> ComputePlacement | None:
        if not request.deployment_id:
            return None
        with self.context.database.session() as session:
            deployment = DeploymentRepository(session).get(
                request.deployment_id,
                workspace_id=request.workspace_id,
            )
        if deployment is None:
            raise InvalidInputError(
                f"deployment {request.deployment_id!r} was not found in the workspace"
            )
        return deployment.resolved_placement

    def _scheduler_pool(
        self,
        request: ComputeCapacityPlacementRequest,
        placement: ComputePlacement,
    ) -> Pool | None:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, request.workspace_id).id
            pools = PoolRepository(session)
            if placement.pool_name:
                selected = pools.get(placement.pool_name, workspace_id=workspace_id)
                if selected is None:
                    raise InvalidInputError(
                        f"attached compute pool {placement.pool_name!r} "
                        "was not found in the workspace"
                    )
                if not _pool_supports(selected, request.requirements):
                    raise InvalidInputError(
                        f"attached compute pool {placement.pool_name!r} "
                        "does not support the requested resources"
                    )
                return selected
            if placement.target is not ComputePlacementTarget.Managed:
                return None
            candidates = [
                pool
                for pool in pools.list(workspace_id=workspace_id)
                if pool.capacity_owner_kind is CapacityOwnerKind.WorkspaceAgent
                and pool.default_eligible
                and _pool_supports(pool, request.requirements)
            ]
        if not candidates:
            return None
        candidates.sort(key=lambda pool: (-pool.priority, pool.capacity_owner_id))
        return candidates[0]


def _pool_supports(pool: Pool, requirements: ComputeResourceRequirements) -> bool:
    if pool.worker_cpu_millicores < requirements.cpu_millicores:
        return False
    if pool.worker_memory_mib < requirements.memory_mb:
        return False
    if requirements.runtime not in pool.worker_runtimes:
        return False
    if requirements.gpu_count == 0:
        return pool.worker_gpu_count == 0
    if pool.worker_gpu_count < requirements.gpu_count:
        return False
    requested_gpu = normalize_gpu_type(requirements.gpu or "")
    return requested_gpu == GPU_ANY or normalize_gpu_type(pool.worker_gpu_type) == requested_gpu


__all__ = [
    "ComputeCapacityPlacementRequest",
    "ComputeCapacityPlacementResult",
    "ComputeCapacityPlacementService",
]
