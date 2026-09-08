from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from compute.request_placement import (
    ComputeCapacityPlacementRequest,
    ComputeCapacityPlacementResult,
)
from shared.compute_policy import ComputeResourceRequirements
from shared.scheduling import SchedulerWorkerRequest, gpu_count_for_capacity


class SchedulerCapacityPlacement(Protocol):
    def place(self, request: ComputeCapacityPlacementRequest) -> ComputeCapacityPlacementResult: ...

    def prepare_capacity(self, request: ComputeCapacityPlacementRequest) -> None: ...


@dataclass(frozen=True, slots=True)
class SchedulerComputePlacement:
    capacity: SchedulerCapacityPlacement

    def place(self, request: SchedulerWorkerRequest) -> SchedulerWorkerRequest:
        """Resolve the pool; capacity controllers choose the unit during acquisition."""
        result = self.capacity.place(_capacity_request(request))
        return request.model_copy(update={"pool_selector": result.pool})

    def prepare_capacity(self, request: SchedulerWorkerRequest) -> None:
        self.capacity.prepare_capacity(_capacity_request(request))


def _capacity_request(request: SchedulerWorkerRequest) -> ComputeCapacityPlacementRequest:
    # GPU model alternatives do not increase the number of cards requested.
    gpu_count = gpu_count_for_capacity(request.gpu, request.gpu_count)
    return ComputeCapacityPlacementRequest(
        workspace_id=request.workspace_id,
        deployment_id=request.deployment_id,
        requested_pool=request.pool_selector,
        region=request.region,
        requirements=ComputeResourceRequirements(
            cpu_millicores=request.cpu_millicores,
            memory_mb=request.memory_mib,
            gpu=list(request.gpu) if gpu_count > 0 else [],
            gpu_count=gpu_count,
            architecture=request.architecture,
            runtime=request.provider_runtime,
        ),
    )


__all__ = ["SchedulerComputePlacement"]
