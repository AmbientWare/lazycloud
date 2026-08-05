from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from database.repositories.apps import DeploymentRepository
from database.repositories.compute import ComputeUnitRepository
from shared.compute_policy import ComputeResourceRequirements, ComputeUnitRecord
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
    ) -> ComputeUnitRecord: ...


class ComputeCapacityPlacementRequest(ContractModel):
    workspace_id: str
    deployment_id: str = ""
    requested_pool: str = ""
    requirements: ComputeResourceRequirements


@dataclass(frozen=True, slots=True)
class ComputeCapacityPlacementResult:
    pool: str


@dataclass(slots=True)
class ComputeCapacityPlacementService:
    context: ComputeContext
    policies: WorkspaceComputePolicyService
    compute: PooledCapacityOwner

    def place(self, request: ComputeCapacityPlacementRequest) -> ComputeCapacityPlacementResult:
        """Name the pool a request lands in, provisioning a unit if none fits.

        The group is the answer; the unit inside it is the capacity
        controllers' to pick. Provisioning happens here only when no existing
        unit in the pool can host the shape and the pool is one a connected
        account feeds — a pool fed only by joined machines has nothing to
        provision into and is left as it is.
        """
        pool = self._machine_pool_for(request)
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, request.workspace_id).id
            units = ComputeUnitRepository(session).list_for_machine_pool(workspace_id, pool)
        if any(_pool_supports(unit, request.requirements) for unit in units):
            return ComputeCapacityPlacementResult(pool=pool)
        connection = self.policies.connection_for_machine_pool(
            workspace=request.workspace_id,
            pool=pool,
        )
        if connection is None:
            return ComputeCapacityPlacementResult(pool=pool)

        policy = self.policies.get_policy(workspace=request.workspace_id)
        aws = policy.aws
        machine_limit = (
            aws.max_gpu_instances if request.requirements.gpu_count > 0 else aws.max_cpu_instances
        )
        self.compute.prepare_pooled_capacity(
            workspace=request.workspace_id,
            requirements=request.requirements,
            region=aws.default_region,
            desired_machines=0,
            workspace_machine_limit=machine_limit,
            root_volume_gib=aws.root_volume_gib,
            idle_timeout_seconds=aws.idle_timeout_seconds,
            allowed_instance_types=aws.allowed_instance_types,
        )
        return ComputeCapacityPlacementResult(pool=pool)

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
