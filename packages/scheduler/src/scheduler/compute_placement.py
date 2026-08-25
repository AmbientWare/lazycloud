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


@dataclass(frozen=True, slots=True)
class SchedulerComputePlacement:
    capacity: SchedulerCapacityPlacement

    def place(self, request: SchedulerWorkerRequest) -> SchedulerWorkerRequest:
        """Resolve the pool this request lands in.

        Only the pool is decided here. Which unit inside it serves the request
        is the capacity controllers' arbitration, so naming one now would leave
        the acquisition loop a single candidate and no failover to run.
        """
        # The canonical count, not the length of the list: a request naming two
        # acceptable models wants one card, and reading it as two asked for
        # hardware nobody has and placed nowhere.
        gpu_count = gpu_count_for_capacity(request.gpu, request.gpu_count)
        result = self.capacity.place(
            ComputeCapacityPlacementRequest(
                workspace_id=request.workspace_id,
                deployment_id=request.deployment_id,
                requested_pool=request.pool_selector,
                requirements=ComputeResourceRequirements(
                    cpu_millicores=request.cpu_millicores,
                    memory_mb=request.memory_mib,
                    gpu=list(request.gpu) if gpu_count > 0 else [],
                    gpu_count=gpu_count,
                    architecture=request.architecture,
                    runtime=request.provider_runtime,
                ),
            )
        )
        return request.model_copy(update={"pool_selector": result.pool})


__all__ = ["SchedulerComputePlacement"]
