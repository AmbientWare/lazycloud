from datetime import datetime

from shared.fleet_capacity import ReserveMachineState
from shared.http.base import HttpModel
from shared.releases import ReleaseMachinePhase


class FleetCapacityResponse(HttpModel):
    cpu_millicores: int
    memory_mib: int
    gpu_count: int


class FleetStateResponse(HttpModel):
    state: ReserveMachineState
    machines: int
    capacity: FleetCapacityResponse
    allocated: FleetCapacityResponse


class FleetMarketResponse(HttpModel):
    preemptible: bool
    gpu_type: str
    warm_free: FleetCapacityResponse
    warm_target: FleetCapacityResponse
    reserve_ready: FleetCapacityResponse
    reserve_target: FleetCapacityResponse
    allocated: FleetCapacityResponse
    states: list[FleetStateResponse]
    reason: str


class FleetPlanResponse(HttpModel):
    generated_at: datetime
    expires_at: datetime
    markets: list[FleetMarketResponse]


class FleetReleaseResponse(HttpModel):
    version: str
    generation: int
    complete: bool
    phases: dict[ReleaseMachinePhase, int]
    pending_capacity_owners: int


class FleetSummaryResponse(HttpModel):
    observed_at: datetime
    plan: FleetPlanResponse | None
    release: FleetReleaseResponse | None


class FleetNodeResponse(HttpModel):
    id: str
    machine_id: str | None
    instance_id: str | None
    provider: str
    region: str
    instance_type: str
    preemptible: bool
    gpu_type: str
    state: ReserveMachineState
    capacity: FleetCapacityResponse
    allocated: FleetCapacityResponse
    containers: int
    ready: bool


class FleetNodeListResponse(HttpModel):
    data: list[FleetNodeResponse]
    next: str
    observed_at: datetime
