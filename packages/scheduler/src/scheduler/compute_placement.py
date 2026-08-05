from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from compute.request_placement import (
    ComputeCapacityPlacementRequest,
    ComputeCapacityPlacementResult,
)
from shared.compute_policy import ComputeResourceRequirements
from shared.scheduling import SchedulerWorkerRequest


class SchedulerCapacityPlacement(Protocol):
    def place(self, request: ComputeCapacityPlacementRequest) -> ComputeCapacityPlacementResult: ...


@dataclass(frozen=True, slots=True)
class SchedulerComputePlacement:
    capacity: SchedulerCapacityPlacement

    def place(self, request: SchedulerWorkerRequest) -> SchedulerWorkerRequest:
        """Resolve the pool this request lands in.

        Only the pool is decided here. Which unit inside it serves the request
        is the capacity controllers' arbitration, so naming one now would leave
        the acquisition loop a single candidate and no failover to run.
        """
        gpu_count = max(request.gpu_count, len(request.gpu_request))
        gpu = request.gpu_type or (request.gpu_request[0] if request.gpu_request else None)
        result = self.capacity.place(
            ComputeCapacityPlacementRequest(
                workspace_id=request.workspace_id,
                deployment_id=request.deployment_id,
                requested_pool=request.pool_selector,
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
        return request.model_copy(update={"pool_selector": result.machine_pool})


__all__ = ["SchedulerComputePlacement"]
