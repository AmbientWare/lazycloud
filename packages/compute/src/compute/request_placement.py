from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from database.repositories.apps import DeploymentRepository
from database.repositories.compute import ComputeUnitRepository
from database.repositories.identity import WorkspaceMemberRepository
from shared.aws_connections import AwsAccountConnection
from shared.compute_policy import ComputeResourceRequirements, ComputeUnitRecord, MachinePool
from shared.container_requests import capacity_with_overhead
from shared.contracts import ContractModel
from shared.errors import InvalidInputError
from shared.gpu import gpu_preference_accepts

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
    pool: MachinePool


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
            units = ComputeUnitRepository(session).list_for_machine_pool(
                workspace_id, MachinePool(pool)
            )
        if any(_pool_supports(unit, request.requirements) for unit in units):
            return ComputeCapacityPlacementResult(pool=MachinePool(pool))
        connection = self.policies.connection_for_machine_pool(
            workspace=request.workspace_id,
            pool=MachinePool(pool),
        )
        if connection is None:
            return ComputeCapacityPlacementResult(pool=MachinePool(pool))

        # Capacity belongs to the account that pays for it, not to the account
        # that asked. A customer on the shared fleet has no connection of their
        # own, so preparing capacity in their workspace looks up a provider that
        # is not there and refuses the request outright — which is worse than
        # the silence it replaced, because it fails work the fleet could have
        # run. Falling back leaves the pre-existing behaviour: land on whatever
        # the pool already has.
        capacity_workspace = self._connection_workspace(connection)
        if capacity_workspace is None:
            return ComputeCapacityPlacementResult(pool=MachinePool(pool))

        configuration = connection.compute
        self.compute.prepare_pooled_capacity(
            workspace=capacity_workspace,
            requirements=request.requirements,
            region=configuration.default_region,
            desired_machines=0,
            root_volume_gib=configuration.root_volume_gib,
            idle_timeout_seconds=configuration.idle_timeout_seconds,
            allowed_instance_types=configuration.allowed_instance_types,
        )
        return ComputeCapacityPlacementResult(pool=MachinePool(pool))

    def _connection_workspace(self, connection: AwsAccountConnection) -> str | None:
        """A workspace owned by the account this connection belongs to.

        The unit a provisioning run creates has to live in a workspace, and it
        is the connection's account that is charged for the machine, so it lives
        in theirs rather than in whoever asked. Which of their workspaces holds
        it does not decide who the capacity serves: a platform-fleet unit serves
        every account, and a private one is compared by owner.
        """
        with self.context.database.session() as session:
            owned = WorkspaceMemberRepository(session).owned_workspace_ids(connection.user_id)
        return min(owned, default=None)

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
