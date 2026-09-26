from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from compute.fleet_resources import Capacity
from compute.request_placement import (
    ComputeCapacityPlacementRequest,
    ComputeCapacityPurchase,
)
from shared.compute_policy import ComputeResourceRequirements
from shared.container_requests import capacity_memory_mib
from shared.scheduling import SchedulerWorkerRequest, gpu_count_for_capacity


class SchedulerCapacityPlacement(Protocol):
    def purchase_candidates(
        self, request: ComputeCapacityPlacementRequest
    ) -> tuple[ComputeCapacityPurchase, ...]: ...


@dataclass(frozen=True, slots=True)
class SchedulerComputePlacement:
    capacity: SchedulerCapacityPlacement

    def purchase_candidates(
        self, request: SchedulerWorkerRequest, *, cohort: Sequence[SchedulerWorkerRequest] = ()
    ) -> tuple[ComputeCapacityPurchase, ...]:
        compatible = tuple(
            Capacity(
                item.cpu_millicores,
                capacity_memory_mib(item.memory_mib),
                gpu_count_for_capacity(item.gpu, item.gpu_count),
            )
            for item in cohort
            if item.container_id != request.container_id
            and item.placement == request.placement
            and item.region == request.region
            and item.availability_zone == request.availability_zone
            and item.architecture == request.architecture
            and item.provider_runtime == request.provider_runtime
            and item.runtime_class == request.runtime_class
            and item.docker_enabled == request.docker_enabled
            and item.preemptible == request.preemptible
            and item.gpu == request.gpu
            and item.required_worker_id == request.required_worker_id
            and item.disk_bytes == request.disk_bytes == 0
            and item.disk_count == request.disk_count == 0
        )
        return self.capacity.purchase_candidates(_capacity_request(request, cohort=compatible))


def _capacity_request(
    request: SchedulerWorkerRequest, *, cohort: tuple[Capacity, ...] = ()
) -> ComputeCapacityPlacementRequest:
    # GPU model alternatives do not increase the number of cards requested.
    gpu_count = gpu_count_for_capacity(request.gpu, request.gpu_count)
    return ComputeCapacityPlacementRequest(
        workspace_id=request.workspace_id,
        deployment_id=request.deployment_id,
        placement=request.placement,
        region=request.region,
        requirements=ComputeResourceRequirements(
            cpu_millicores=request.cpu_millicores,
            memory_mb=request.memory_mib,
            gpu=list(request.gpu) if gpu_count > 0 else [],
            gpu_count=gpu_count,
            architecture=request.architecture,
            preemptible=request.preemptible,
            availability_zone=request.availability_zone,
            runtime=request.provider_runtime,
        ),
        preferred_availability_zone=request.preferred_availability_zone,
        cohort=cohort,
    )


__all__ = ["SchedulerComputePlacement"]
