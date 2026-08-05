from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from compute.request_placement import (
    ComputeCapacityPlacementRequest,
    ComputeCapacityPlacementResult,
)
from scheduler.capacity_reservations import (
    CapacityAcquisitionStatus,
    CapacityProvisioningReservation,
    ComputePoolCapacityController,
)
from scheduler.compute_placement import SchedulerComputePlacement
from scheduler.state import SchedulerWorkerRequest
from shared.capacity import (
    CapacityAcquisitionRequest,
    CapacityAcquisitionResult,
    CapacityOwnerKind,
    CapacityOwnerSource,
    CapacityPoolSizingSnapshot,
    CapacityReleaseRequest,
)
from shared.capacity import (
    CapacityAcquisitionStatus as ComputeCapacityAcquisitionStatus,
)
from shared.compute_fleet import Pool
from shared.compute_policy import (
    ComputePlacement,
    ComputePlacementSource,
    ComputePlacementTarget,
)
from shared.scheduling import SchedulerWorkerRecord

_OWNER_ID = "11111111-1111-4111-8111-111111111111"


def test_scheduler_forwards_typed_ad_hoc_placement_to_capacity_owner() -> None:
    capacity = _RecordingCapacity()
    request = SchedulerWorkerRequest(
        workspace_id="workspace-1",
        stub_id="stub-1",
        container_id="container-1",
        requested_placement=ComputePlacementTarget.Aws,
        cpu_millicores=1_000,
        memory_mib=2_048,
    )

    placed = SchedulerComputePlacement(capacity).place(request)

    assert capacity.requests[0].deployment_id == ""
    assert capacity.requests[0].requested_placement is ComputePlacementTarget.Aws
    assert capacity.requests[0].requirements.cpu_millicores == 1_000
    assert capacity.requests[0].requirements.memory_mb == 2_048
    assert placed.pool_selector == "internal-aws-cpu"
    assert placed.capacity_owner_id == _OWNER_ID
    assert placed.requested_placement is ComputePlacementTarget.Aws
    assert placed.placement_source is ComputePlacementSource.WorkloadOverride

    compute = _RequestedComputeCapacity()
    controller = ComputePoolCapacityController(
        workspace_id="workspace-1",
        pool=_internal_aws_pool(),
        compute=compute,
        workers=_NoWorkers(),
    )
    assert controller.accepts(placed)
    assert not controller.accepts(placed.model_copy(update={"capacity_owner_id": ""}))

    now = datetime(2026, 7, 22, tzinfo=UTC)
    acquisition_shape = controller.reservation_shape(placed)
    reservation = CapacityProvisioningReservation(
        id="22222222-2222-4222-8222-222222222222",
        capacity_owner_id=_OWNER_ID,
        pool_name="internal-aws-cpu",
        owner_kind=CapacityOwnerKind.PooledProvider,
        acquisition_shape=acquisition_shape,
        schedulable_shape=acquisition_shape.model_copy(update={"memory_mib": 15_500}),
        operation_id="33333333-3333-4333-8333-333333333333",
        registration_deadline_at=now + timedelta(minutes=10),
        created_at=now,
        updated_at=now,
    )
    planned = controller.ensure_capacity(
        reservation,
        owner_reservations=(reservation,),
        now=now,
    )

    assert planned.status is CapacityAcquisitionStatus.Requested
    assert len(compute.plans) == 1
    assert compute.plans[0].capacity_owner_id == _OWNER_ID
    assert compute.plans[0].reservation_id == reservation.id
    assert compute.plans[0].shape.memory_mib == 16_384


@dataclass(slots=True)
class _RecordingCapacity:
    requests: list[ComputeCapacityPlacementRequest] = field(default_factory=list)

    def place(self, request: ComputeCapacityPlacementRequest) -> ComputeCapacityPlacementResult:
        self.requests.append(request)
        return ComputeCapacityPlacementResult(
            placement=ComputePlacement(
                target=ComputePlacementTarget.Aws,
                source=ComputePlacementSource.WorkloadOverride,
                provider="aws",
                region="us-east-1",
                pool_name="internal-aws-cpu",
            ),
            capacity_owner_id=_OWNER_ID,
        )


@dataclass(slots=True)
class _RequestedComputeCapacity:
    plans: list[CapacityAcquisitionRequest] = field(default_factory=list)

    def pool_sizing_snapshot(self, capacity_owner_id: str) -> CapacityPoolSizingSnapshot:
        raise AssertionError(f"unexpected sizing snapshot read for {capacity_owner_id}")

    def ensure_capacity(
        self,
        request: CapacityAcquisitionRequest,
        *,
        minimum_unit: int = 0,
    ) -> CapacityAcquisitionResult:
        _ = minimum_unit
        self.plans.append(request)
        return CapacityAcquisitionResult(
            status=ComputeCapacityAcquisitionStatus.Requested,
            capacity_owner_id=request.capacity_owner_id,
            reservation_id=request.reservation_id,
            desired_unit=1,
        )

    def release_acquired_capacity(
        self,
        request: CapacityReleaseRequest,
    ) -> CapacityAcquisitionResult:
        raise AssertionError(f"unexpected capacity release for {request.operation_id}")


@dataclass(frozen=True, slots=True)
class _NoWorkers:
    def list_workers(self) -> list[SchedulerWorkerRecord]:
        return []


def _internal_aws_pool() -> Pool:
    return Pool(
        name="internal-aws-cpu",
        provider="aws",
        capacity_owner_id=_OWNER_ID,
        capacity_owner_kind=CapacityOwnerKind.PooledProvider,
        capacity_owner_source=CapacityOwnerSource.Provider,
        max_workers=10,
        scaling_enabled=True,
        default_eligible=False,
        worker_cpu_millicores=4_000,
        worker_memory_mib=16_384,
    )
