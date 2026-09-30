from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.contracts import ContractModel
from shared.enums import StringEnum


class CapacityMaintenanceKind(StringEnum):
    Runtime = "runtime"
    ReserveRefresh = "reserve_refresh"


class CapacityMaintenancePhase(StringEnum):
    Planned = "planned"
    Preparing = "preparing"
    Draining = "draining"
    Verifying = "verifying"
    Retiring = "retiring"
    Complete = "complete"
    Failed = "failed"


class CapacityMaintenanceRecord(ContractModel):
    id: str
    pool_id: str
    source_machine_id: str
    release_generation: int = Field(gt=0)
    kind: CapacityMaintenanceKind
    phase: CapacityMaintenancePhase = CapacityMaintenancePhase.Planned
    surge_machines: int = Field(default=0, ge=0)
    running_cpu_millicores: int = Field(default=0, ge=0)
    hourly_cost_micros: int | None = Field(default=None, ge=0)
    replacement_machine_id: str | None = None
    reserved_cpu_millicores: int = Field(default=0, ge=0)
    reserved_memory_mib: int = Field(default=0, ge=0)
    reserved_gpu_count: int = Field(default=0, ge=0)
    reserved_disk_bytes: int = Field(default=0, ge=0)
    reserved_disk_volumes: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    reason: str = Field(default="", max_length=512)


class CapacityMaintenanceCommitments(ContractModel):
    operations: int = 0
    surge_machines: int = 0
    running_cpu_millicores: int = 0
    hourly_cost_micros: int = 0
    unknown_cost_operations: int = 0


__all__ = [
    "CapacityMaintenanceCommitments",
    "CapacityMaintenanceKind",
    "CapacityMaintenancePhase",
    "CapacityMaintenanceRecord",
]
