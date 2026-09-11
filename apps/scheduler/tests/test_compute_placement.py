from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from compute.request_placement import (
    ComputeCapacityPlacementRequest,
    ComputeCapacityPlacementResult,
    ComputeCapacityPurchase,
)
from scheduler.capacity_reservations import (
    CapacityAcquisitionStatus,
    CapacityProvisioningReservation,
    ComputeUnitCapacityController,
)
from scheduler.compute_placement import SchedulerComputePlacement
from scheduler.state import SchedulerWorkerRequest
from shared.capacity import (
    CapacityAcquisitionRequest,
    CapacityAcquisitionResult,
    CapacityFulfillmentRequest,
    CapacityOwnerKind,
    CapacityOwnerSource,
    CapacityPoolSizingSnapshot,
    CapacityReleaseRequest,
)
from shared.capacity import (
    CapacityAcquisitionStatus as ComputeCapacityAcquisitionStatus,
)
from shared.compute_policy import (
    ComputeUnitRecord,
    MachinePool,
    UnitName,
)
from shared.scheduling import SchedulerWorkerRecord

_OWNER_ID = "11111111-1111-4111-8111-111111111111"
_WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
_SIBLING_OWNER_ID = "33333333-3333-4333-8333-333333333333"


def test_scheduler_stamps_the_resolved_pool_and_every_unit_in_it_accepts() -> None:
    """The request carries a pool, and each unit feeding it is a candidate."""
    capacity = _RecordingCapacity()
    request = SchedulerWorkerRequest(
        workspace_id="workspace-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=1_000,
        memory_mib=2_048,
    )

    placed = SchedulerComputePlacement(capacity).place(request)

    assert capacity.requests[0].deployment_id == ""
    assert capacity.requests[0].requirements.cpu_millicores == 1_000
    assert capacity.requests[0].requirements.memory_mb == 2_048
    assert placed.pool_selector == "aws"

    compute = _RequestedComputeCapacity()
    controller = ComputeUnitCapacityController(
        workspace_id="workspace-1",
        unit=_internal_aws_pool(),
        compute=compute,
        workers=_NoWorkers(),
    )
    assert controller.accepts(placed)
    # A second unit feeding the same group is equally a candidate; that is what
    # gives the acquisition loop something to fail over to.
    sibling = ComputeUnitCapacityController(
        workspace_id="workspace-1",
        unit=_internal_aws_pool().model_copy(
            update={
                "id": _SIBLING_OWNER_ID,
                "capacity_owner_id": _SIBLING_OWNER_ID,
                "name": "internal-aws-cpu-west",
            }
        ),
        compute=compute,
        workers=_NoWorkers(),
    )
    assert sibling.accepts(placed)
    # A unit feeding a different group is not.
    other_group = ComputeUnitCapacityController(
        workspace_id="workspace-1",
        unit=_internal_aws_pool().model_copy(update={"pool": "another-group"}),
        compute=compute,
        workers=_NoWorkers(),
    )
    assert not other_group.accepts(placed)

    now = datetime(2026, 7, 22, tzinfo=UTC)
    acquisition_shape = controller.reservation_shape(placed)
    reservation = CapacityProvisioningReservation(
        id="22222222-2222-4222-8222-222222222222",
        capacity_owner_id=_OWNER_ID,
        pool=MachinePool("internal-aws-cpu"),
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
        return ComputeCapacityPlacementResult(pool=MachinePool("aws"))

    def purchase_candidates(
        self, request: ComputeCapacityPlacementRequest
    ) -> tuple[ComputeCapacityPurchase, ...]:
        raise AssertionError("pool selection must not prepare capacity")


@dataclass(slots=True)
class _RequestedComputeCapacity:
    plans: list[CapacityAcquisitionRequest] = field(default_factory=list)

    def fulfill_acquired_capacity(self, request: CapacityFulfillmentRequest) -> None:
        raise AssertionError(f"unexpected capacity fulfillment for {request.operation_id}")

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


def _internal_aws_pool() -> ComputeUnitRecord:
    return ComputeUnitRecord(
        id=_OWNER_ID,
        workspace_id=_WORKSPACE_ID,
        name=UnitName("internal-aws-cpu"),
        pool=MachinePool("aws"),
        provider="aws",
        capacity_owner_id=_OWNER_ID,
        capacity_owner_kind=CapacityOwnerKind.PooledProvider,
        capacity_owner_source=CapacityOwnerSource.Provider,
        max_machines=10,
        scaling_enabled=True,
        default_eligible=False,
        worker_cpu_millicores=4_000,
        worker_memory_mib=16_384,
    )
