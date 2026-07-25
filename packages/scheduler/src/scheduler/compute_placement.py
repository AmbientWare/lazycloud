from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from compute.request_placement import (
    ComputeCapacityPlacementRequest,
    ComputeCapacityPlacementResult,
)
from shared.compute_policy import (
    ComputePlacementSource,
    ComputeResourceRequirements,
)
from shared.scheduling import SchedulerWorkerRequest


class SchedulerCapacityPlacement(Protocol):
    def place(self, request: ComputeCapacityPlacementRequest) -> ComputeCapacityPlacementResult: ...


@dataclass(frozen=True, slots=True)
class SchedulerComputePlacement:
    capacity: SchedulerCapacityPlacement

    def place(self, request: SchedulerWorkerRequest) -> SchedulerWorkerRequest:
        gpu_count = max(request.gpu_count, len(request.gpu_request))
        gpu = request.gpu_type or (request.gpu_request[0] if request.gpu_request else None)
        result = self.capacity.place(
            ComputeCapacityPlacementRequest(
                workspace_id=request.workspace_id,
                deployment_id=request.deployment_id,
                attached_pool=(
                    request.pool_selector
                    if request.placement_source in {None, ComputePlacementSource.AttachedPool}
                    else ""
                ),
                requested_placement=request.requested_placement,
                requirements=ComputeResourceRequirements(
                    cpu_millicores=request.cpu_millicores,
                    memory_mb=request.memory_mib,
                    gpu=gpu if gpu_count > 0 else None,
                    gpu_count=gpu_count,
                    architecture=request.architecture,
                    runtime=request.provider_runtime,
                ),
            )
        )
        placement = result.placement
        return request.model_copy(
            update={
                "pool_selector": placement.pool_name,
                "capacity_owner_id": result.capacity_owner_id or "",
                "placement_source": placement.source,
            }
        )


__all__ = ["SchedulerComputePlacement"]
