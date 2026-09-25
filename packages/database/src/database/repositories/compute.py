from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import pinned_container
from database.tables.aws_connections import AwsAccountConnectionTable
from database.tables.capacity_recovery import CapacityRecoveryTable
from database.tables.compute import (
    ComputeCapacityOperationTable,
    ComputeJoinCredentialTable,
    ComputeMachineEnrollmentTable,
    ComputeNodeShapeTable,
    ComputeProviderInstanceTable,
    ComputeUnitTable,
)
from database.tables.identity import WorkspaceMemberTable, WorkspaceTable
from database.tables.orchestration import ContainerTable
from pydantic import Field
from shared.capacity import (
    TERMINAL_REASON_MAX_LENGTH,
    CapacityAcquisitionShape,
    CapacityFailureCode,
    CapacityOperationStatus,
    CapacityOwnerKind,
)
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputeCredentialStatus,
    ComputeMachineEnrollmentStatus,
    ComputePreflightCheck,
    MachineReadinessPhase,
)
from shared.compute_policy import (
    ComputeUnitPhase,
    ComputeUnitProviderState,
    ComputeUnitRecord,
    ComputeUnitVisibility,
)
from shared.compute_reconciliation import ComputeReconciliationKind
from shared.container_requests import CONTAINER_MEMORY_RESERVATION_PERCENT
from shared.containers import LIVE_CONTAINER_STATUSES
from shared.contracts import ContractModel
from shared.errors import ConflictError
from shared.identity import WorkspaceRole, WorkspaceStatus
from shared.placement import Placement
from shared.supplier_costs import SupplierCostTerms, SupplierCpuUnit
from shared.timestamps import to_utc, to_utc_or_none, utc_now
from sqlalchemy import (
    BigInteger,
    Select,
    String,
    and_,
    case,
    cast,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement


class ComputeCapacityOperationRecord(ContractModel):
    id: str
    workspace_id: str
    pool_id: str
    capacity_owner_id: str
    reservation_id: str
    operation_id: str
    demand_container_id: str | None = None
    desired_unit: int = Field(ge=1)
    status: CapacityOperationStatus
    target_machine_id: str | None = None
    fulfilled_at: datetime | None = None
    provider_instance_id: str | None = None
    previous_desired_unit: int = Field(default=0, ge=0)
    release_desired_unit: int | None = Field(default=None, ge=0)
    owns_capacity: bool = False
    join_attempt: int = Field(default=1, ge=1)
    shape: CapacityAcquisitionShape
    failure_code: CapacityFailureCode | None = None
    failure_count: int = Field(default=0, ge=0)
    last_error: str = Field(default="", max_length=TERMINAL_REASON_MAX_LENGTH)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ComputeCapacityOperationSizingRecord:
    operation_id: str
    desired_unit: int
    status: CapacityOperationStatus
    owns_capacity: bool
    failure_count: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ComputeOfferState:
    id: str
    desired_machines: int
    observed_machines: int
    phase: ComputeUnitPhase
    provider_state: ComputeUnitProviderState
    registration_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ComputeCapacityOperationHistorySummary:
    peak_desired_unit: int
    last_requested_at: datetime | None


class ComputeProviderInstanceRecord(ContractModel):
    id: str
    provider: str
    offer_id: str
    status: str
    source: str
    pool_id: str | None = None
    instance_type: str | None = None
    instance_id: str | None = None
    machine_id: str | None = None
    gpu: str | None = None
    gpu_count: int = 0
    cpu_millicores: int = 0
    memory_mb: int = 0
    cost_terms: SupplierCostTerms = Field(default_factory=SupplierCostTerms)
    storage_mib: int | None = Field(default=None, ge=0)
    supplier_cpu_unit: SupplierCpuUnit = SupplierCpuUnit.Unknown
    supplier_cpu_count: int | None = Field(default=None, ge=0)
    committed_micros: int = 0
    expires_at: datetime | None = None
    billing_renewal_at: datetime | None = None
    billing_started_at: datetime | None = None
    # What the platform concluded about the node. A node cannot observe that it
    # serves workloads, so these are stamped by the reconcile that watches it
    # rather than by anything the machine says. They are what lets the reclaim
    # tell a machine that never worked from one that worked and stopped, which
    # the machine's lifecycle phase alone cannot express.
    first_enrolled_at: datetime | None = None
    first_served_at: datetime | None = None
    last_served_at: datetime | None = None
    unserved_observations: int = Field(default=0, ge=0)
    launch_attempt: int = Field(default=1, ge=1)
    architecture: str = ""
    runtime: str = ""
    region: str = ""
    availability_zone: str = ""
    storage_volume_ids: tuple[str, ...] = ()
    booted_template_version: str = ""
    # The agent binary and worker image a stopped reserve proved it holds when its
    # preparation last completed. Empty until then, and for a used machine whose
    # agent was not current when it stopped.
    prepared_agent_sha256: str = ""
    prepared_worker_image: str = ""
    missing_since: datetime | None = None
    provider_storage_destroyed_at: datetime | None = None
    terminating_reason: str = ""
    terminated_reason: str = ""
    status_message: str = ""
    last_error: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ComputeReserveInstance:
    """A stopped platform reserve machine."""

    pool_id: str
    instance_id: str
    machine_id: str


@dataclass(frozen=True, slots=True)
class PlatformReserveUnitRow:
    id: str
    workspace_id: str
    provider_ref: str
    preemptible: bool
    gpu_type: str
    gpu_count: int
    cpu_millicores: int
    memory_mib: int
    reported_memory_mib: int
    desired: int
    stopped: int
    retiring_stopped: int
    retained: int
    observed: int
    provider_committed: int
    phase: ComputeUnitPhase
    degraded_reason: str | None
    degraded_at: datetime | None
    last_capacity_failure_at: datetime | None
    registration_timeout_seconds: int
    replacement_machine_id: str
    billing_minimum_seconds: int | None


@dataclass(frozen=True, slots=True)
class PlatformReserveInstanceRow:
    unit_id: str
    status: str
    instance_id: str | None
    machine_id: str | None
    availability_zone: str
    billing_started_at: datetime | None
    missing: bool
    capacity_state: AgentCapacityState | None
    protected: bool
    containers: int
    pinned: int
    load_cpu_millicores: int
    load_memory_mib: int
    load_gpu_count: int


@dataclass(frozen=True, slots=True)
class StoppedReserveUnitRow:
    id: str
    preemptible: bool
    gpu_type: str
    cpu_millicores: int
    memory_mib: int
    reported_memory_mib: int
    gpu_count: int
    stopped: int
    resumable: bool


@dataclass(frozen=True, slots=True)
class PlatformReserveRows:
    units: tuple[PlatformReserveUnitRow, ...]
    instances: tuple[PlatformReserveInstanceRow, ...]


@dataclass(frozen=True, slots=True)
class ComputeProviderInstanceSizingSummary:
    open_count: int
    last_released_at: datetime | None


@dataclass(frozen=True, slots=True)
class ComputeUnitSizingRecord:
    id: str
    capacity_owner_kind: CapacityOwnerKind
    desired_machines: int


class ComputeJoinCredentialRecord(ContractModel):
    id: str
    token_hash: str
    user_id: str | None
    """Account the machine joining with this credential will belong to.

    Resolved from the owner of the minting workspace, and the only thing placement
    compares. A machine serves every workspace this account owns.
    """
    workspace_id: str
    capacity_owner_id: str
    """Provisioning unit that issued this credential.

    A machine joining with it is bought by that unit, which is what keeps an
    auto-scaling drain from selecting a machine some other unit owns.
    """
    placement: Placement
    """Where the joining machine lands, copied from the issuing unit."""
    machine_id: str = ""
    created_by_token_id: str | None = None
    status: ComputeCredentialStatus = ComputeCredentialStatus.Active
    max_uses: int = 1
    use_count: int = 0
    expires_at: datetime
    revoked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    def revoke(self, *, now: datetime) -> ComputeJoinCredentialRecord:
        return self.model_copy(
            update={
                "status": ComputeCredentialStatus.Revoked,
                "revoked_at": now,
                "updated_at": now,
            }
        )

    def with_use_count(self, use_count: int, *, now: datetime) -> ComputeJoinCredentialRecord:
        return self.model_copy(update={"use_count": use_count, "updated_at": now})


class ComputeMachineCredentialRecord(ContractModel):
    id: str
    user_id: str | None
    credential_generation: int
    status: ComputeMachineEnrollmentStatus
    schedulable: bool
    capacity_state: AgentCapacityState
    capacity_reason: str
    capacity_observed_at: datetime | None
    capacity_notice_at: datetime | None


class ComputeMachineEnrollmentRecord(ContractModel):
    id: str
    user_id: str | None
    """Account this machine belongs to, stamped from the credential that enrolled it."""
    workspace_id: str
    capacity_owner_id: str
    placement: Placement
    machine_id: str
    machine_fingerprint_hash: str
    join_credential_id: str | None = None
    credential_hash: str
    credential_generation: int = 1
    tunnel_public_key_sha256: str = Field(default="", strict=True, pattern=r"^(?:[0-9a-f]{64})?$")
    status: ComputeMachineEnrollmentStatus = ComputeMachineEnrollmentStatus.Active
    preflight_passed: bool = False
    heartbeat_confirmed: bool = False
    schedulable: bool = False
    capacity_state: AgentCapacityState = AgentCapacityState.Available
    capacity_reason: str = ""
    capacity_observed_at: datetime | None = None
    capacity_notice_at: datetime | None = None
    readiness_phase: MachineReadinessPhase = MachineReadinessPhase.Joining
    hostname: str = ""
    os: str = ""
    arch: str = ""
    cpu_count: int = 0
    cpu_millicores: int = 0
    memory_mb: int = 0
    gpus: list[str] = Field(default_factory=list)
    gpu_ids: list[str] = Field(default_factory=list)
    gpu_count: int = 0
    executor: str = ""
    preflight: list[ComputePreflightCheck] = Field(default_factory=list)
    agent_version: str = ""
    last_join_at: datetime
    last_heartbeat_at: datetime | None = None
    last_disconnect_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ComputeMachineCapacityInterruptionRecord:
    enrollment_id: str
    credential_generation: int
    workspace_id: str
    placement: Placement
    machine_id: str
    state: AgentCapacityState
    reason: str
    observed_at: datetime
    notice_at: datetime | None


class ComputeMachineEnrollmentCreate(ContractModel):
    user_id: str | None
    workspace_id: str
    capacity_owner_id: str
    placement: Placement
    machine_id: str
    machine_fingerprint_hash: str
    join_credential_id: str | None = None
    credential_hash: str
    credential_generation: int = 1
    status: ComputeMachineEnrollmentStatus = ComputeMachineEnrollmentStatus.Active
    preflight_passed: bool = False
    heartbeat_confirmed: bool = False
    schedulable: bool = False
    capacity_state: AgentCapacityState = AgentCapacityState.Available
    capacity_reason: str = ""
    capacity_observed_at: datetime | None = None
    capacity_notice_at: datetime | None = None
    readiness_phase: MachineReadinessPhase = MachineReadinessPhase.Joining
    hostname: str = ""
    os: str = ""
    arch: str = ""
    cpu_count: int = 0
    cpu_millicores: int = 0
    memory_mb: int = 0
    gpus: list[str] = Field(default_factory=list)
    gpu_ids: list[str] = Field(default_factory=list)
    gpu_count: int = 0
    executor: str = ""
    preflight: list[ComputePreflightCheck] = Field(default_factory=list)
    agent_version: str = ""
    last_join_at: datetime
    last_heartbeat_at: datetime | None = None
    last_disconnect_at: datetime | None = None
    revoked_at: datetime | None = None

    def update_record(
        self,
        existing: ComputeMachineEnrollmentRecord,
        *,
        updated_at: datetime,
    ) -> ComputeMachineEnrollmentRecord:
        """Apply a snapshot, retaining the tunnel key within its credential generation."""
        return existing.model_copy(
            update={
                **dict(self),
                "tunnel_public_key_sha256": (
                    ""
                    if self.credential_generation > existing.credential_generation
                    else existing.tunnel_public_key_sha256
                ),
                "updated_at": updated_at,
            }
        )


def _compute_unit_record(row: ComputeUnitTable) -> ComputeUnitRecord:
    return ComputeUnitRecord.model_validate(
        {
            "capacity_owner_id": row.capacity_owner_id,
            "capacity_owner_kind": row.capacity_owner_kind,
            "capacity_owner_source": row.capacity_owner_source,
            "id": row.id,
            "workspace_id": row.workspace_id,
            "name": row.name,
            "placement": Placement.parse(row.placement),
            "provider": row.provider,
            "selector": row.selector,
            "status": row.status,
            "source": row.source,
            "expires_at": to_utc_or_none(row.expires_at),
            "provider_ref": row.provider_ref,
            "provider_connection_id": row.provider_connection_id,
            "platform_fleet": row.platform_fleet,
            "capacity_mode": row.capacity_mode,
            "visibility": row.visibility,
            "region": row.region,
            "offer_id": row.offer_id,
            "capability_key": row.capability_key,
            "offer_cost_terms": SupplierCostTerms.model_validate(row.offer_cost_terms)
            if row.offer_cost_terms is not None
            else None,
            "offer_storage_mib": row.offer_storage_mib,
            "offer_availability_zone": row.offer_availability_zone,
            "supplier_cpu_unit": row.supplier_cpu_unit,
            "supplier_cpu_count": row.supplier_cpu_count,
            "desired_machines": row.desired_machines,
            "stopped_machines": row.stopped_machines,
            "retiring_stopped_machines": row.retiring_stopped_machines,
            "initial_machines": row.initial_machines,
            "min_machines": row.min_machines,
            "max_machines": row.max_machines,
            "observed_machines": row.observed_machines,
            "replacement_machine_id": row.replacement_machine_id,
            "replacement_template_version": row.replacement_template_version,
            "generation": row.generation,
            "phase": row.phase,
            "provider_state": ComputeUnitProviderState(
                resource_id=row.provider_resource_id,
                revision=row.provider_state_revision,
                committed_machines=row.provider_committed_machines,
                attributes=row.provider_attributes,
                degraded_reason=row.degraded_reason,
                degraded_at=to_utc_or_none(row.degraded_at),
                last_capacity_failure_at=to_utc_or_none(row.last_capacity_failure_at),
                launch_attempt_baseline=row.launch_attempt_baseline,
            ),
            "scaling_enabled": row.scaling_enabled,
            "priority": row.priority,
            "min_free_cpu_millicores": row.min_free_cpu_millicores,
            "min_free_memory_mib": row.min_free_memory_mib,
            "min_free_gpu_count": row.min_free_gpu_count,
            "worker_cpu_millicores": row.worker_cpu_millicores,
            "worker_memory_mib": row.worker_memory_mib,
            "worker_gpu_type": row.worker_gpu_type,
            "worker_gpu_count": row.worker_gpu_count,
            "worker_runtimes": row.worker_runtimes,
            "worker_preemptible": row.worker_preemptible,
            "idle_drain_timeout_seconds": row.idle_drain_timeout_seconds,
            "scale_up_cooldown_seconds": row.scale_up_cooldown_seconds,
            "scale_down_cooldown_seconds": row.scale_down_cooldown_seconds,
            "registration_timeout_seconds": row.registration_timeout_seconds,
            "root_volume_gib": row.root_volume_gib,
            "fallback": row.fallback,
            "created_at": to_utc(row.created_at),
        }
    )


@dataclass(slots=True)
class ComputeUnitRepository:
    session: Session

    def platform_reserve_rows(self) -> PlatformReserveRows:
        """Every platform unit holding capacity, its live machines, and their load.

        One statement, because the reserve planner reads it every minute. Terminal
        instances and retired units stay out, and load is summed from the live
        container reservations on each machine through the partial live index.
        """
        unit = ComputeUnitTable
        instance = ComputeProviderInstanceTable
        container = ContainerTable
        enrollment = ComputeMachineEnrollmentTable
        recovery = CapacityRecoveryTable
        reserved_memory = (
            container.scheduling_memory_mib * CONTAINER_MEMORY_RESERVATION_PERCENT + 99
        ) / 100
        load = (
            select(
                container.runtime_machine_id.label("machine_id"),
                func.count().label("containers"),
                func.count().filter(pinned_container()).label("pinned"),
                func.sum(container.scheduling_cpu_millicores).label("cpu"),
                func.sum(reserved_memory).label("memory"),
                func.sum(container.scheduling_gpu_count).label("gpu"),
            )
            .where(
                container.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
                container.runtime_machine_id != "",
            )
            .group_by(container.runtime_machine_id)
            .subquery()
        )
        protected = or_(
            cast(instance.machine_id, String) == unit.replacement_machine_id,
            exists().where(
                recovery.completed_at.is_(None),
                or_(
                    recovery.source_machine_id == instance.machine_id,
                    recovery.replacement_machine_id == instance.machine_id,
                ),
            ),
        )
        rows = self.session.execute(
            select(
                unit.id,
                unit.workspace_id,
                unit.provider_ref,
                unit.worker_preemptible,
                unit.worker_gpu_type,
                unit.worker_gpu_count,
                unit.worker_cpu_millicores,
                unit.worker_memory_mib,
                unit.desired_machines,
                unit.stopped_machines,
                unit.retiring_stopped_machines,
                unit.min_machines,
                unit.observed_machines,
                unit.provider_committed_machines,
                unit.phase,
                unit.degraded_reason,
                unit.degraded_at,
                unit.last_capacity_failure_at,
                unit.registration_timeout_seconds,
                unit.replacement_machine_id,
                cast(unit.offer_cost_terms.op("->>")("billing_minimum_seconds"), BigInteger),
                instance.status,
                instance.instance_id,
                instance.machine_id,
                instance.availability_zone,
                instance.billing_started_at,
                instance.missing_since.is_not(None),
                enrollment.capacity_state,
                protected,
                func.coalesce(load.c.containers, 0),
                func.coalesce(load.c.pinned, 0),
                func.coalesce(load.c.cpu, 0),
                func.coalesce(load.c.memory, 0),
                func.coalesce(load.c.gpu, 0),
                _reported_memory(),
            )
            .select_from(unit)
            .outerjoin(ComputeNodeShapeTable, _same_shape(unit))
            .outerjoin(
                instance,
                and_(instance.pool_id == unit.id, instance.status.not_in(("deleted", "failed"))),
            )
            .outerjoin(
                enrollment,
                and_(
                    enrollment.machine_id == instance.machine_id,
                    enrollment.workspace_id == unit.workspace_id,
                    enrollment.status == ComputeMachineEnrollmentStatus.Active.value,
                ),
            )
            .outerjoin(load, load.c.machine_id == cast(instance.machine_id, String))
            .where(
                unit.visibility == ComputeUnitVisibility.Internal.value,
                unit.platform_fleet.is_(True),
                unit.phase != ComputeUnitPhase.Deleted.value,
                or_(
                    instance.id.is_not(None),
                    unit.desired_machines > 0,
                    unit.stopped_machines > 0,
                    unit.retiring_stopped_machines > 0,
                    unit.min_machines > 0,
                ),
            )
            .order_by(unit.id, instance.id)
        ).tuples()
        units: dict[str, PlatformReserveUnitRow] = {}
        instances: list[PlatformReserveInstanceRow] = []
        for row in rows:
            unit_id = str(row[0])
            if unit_id not in units:
                units[unit_id] = PlatformReserveUnitRow(
                    id=unit_id,
                    workspace_id=str(row[1]),
                    provider_ref=row[2],
                    preemptible=row[3],
                    gpu_type=row[4],
                    gpu_count=row[5],
                    cpu_millicores=row[6],
                    memory_mib=row[7],
                    reported_memory_mib=row[34],
                    desired=row[8],
                    stopped=row[9],
                    retiring_stopped=row[10],
                    retained=row[11],
                    observed=row[12],
                    provider_committed=row[13],
                    phase=ComputeUnitPhase(row[14]),
                    degraded_reason=row[15],
                    degraded_at=to_utc_or_none(row[16]),
                    last_capacity_failure_at=to_utc_or_none(row[17]),
                    registration_timeout_seconds=row[18],
                    replacement_machine_id=row[19],
                    billing_minimum_seconds=row[20],
                )
            if row[21] is None:
                continue
            instances.append(
                PlatformReserveInstanceRow(
                    unit_id=unit_id,
                    status=row[21],
                    instance_id=row[22],
                    machine_id=str(row[23]) if row[23] is not None else None,
                    availability_zone=row[24],
                    billing_started_at=to_utc_or_none(row[25]),
                    missing=bool(row[26]),
                    capacity_state=AgentCapacityState(row[27]) if row[27] is not None else None,
                    protected=bool(row[28]),
                    containers=int(row[29]),
                    pinned=int(row[30]),
                    load_cpu_millicores=int(row[31]),
                    load_memory_mib=int(row[32]),
                    load_gpu_count=int(row[33]),
                )
            )
        return PlatformReserveRows(units=tuple(units.values()), instances=tuple(instances))

    def record_node_memory(self, unit_id: str, memory_mib: int) -> None:
        """Keep the least memory a machine of this unit's shape has reported."""
        if memory_mib <= 0:
            return
        unit = ComputeUnitTable
        shape = ComputeNodeShapeTable
        statement = postgresql_insert(shape).from_select(
            ["cpu_millicores", "memory_mib", "gpu_count", "reported_memory_mib"],
            select(
                unit.worker_cpu_millicores,
                unit.worker_memory_mib,
                unit.worker_gpu_count,
                literal(memory_mib, BigInteger),
            ).where(unit.id == unit_id),
        )
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[shape.cpu_millicores, shape.memory_mib, shape.gpu_count],
                set_={
                    "reported_memory_mib": func.least(
                        shape.reported_memory_mib, statement.excluded.reported_memory_mib
                    )
                },
            )
        )

    def reported_node_memory(self) -> dict[tuple[int, int, int], int]:
        """The least memory reported for each nominal CPU, memory and card count."""
        shape = ComputeNodeShapeTable
        return {
            (cpu, memory, gpu): int(reported)
            for cpu, memory, gpu, reported in self.session.execute(
                select(
                    shape.cpu_millicores,
                    shape.memory_mib,
                    shape.gpu_count,
                    shape.reported_memory_mib,
                )
            ).tuples()
        }

    def platform_running_cpu_millicores(self) -> int:
        """Nominal CPU the platform CPU fleet runs, a replacement's surge included."""
        unit = ComputeUnitTable
        surge = case((func.coalesce(unit.replacement_machine_id, "") != "", 1), else_=0)
        return int(
            self.session.scalar(
                select(
                    func.coalesce(
                        func.sum((unit.desired_machines + surge) * unit.worker_cpu_millicores), 0
                    )
                ).where(
                    unit.visibility == ComputeUnitVisibility.Internal.value,
                    unit.platform_fleet.is_(True),
                    unit.worker_gpu_count == 0,
                    unit.phase != ComputeUnitPhase.Deleted.value,
                    or_(unit.desired_machines > 0, surge > 0),
                )
            )
            or 0
        )

    def platform_stopped_reserves(
        self, *, preemptible: bool, gpu_type: str
    ) -> list[tuple[str, int, int, int, int, int]]:
        """Each of a market's units holding stopped reserves: its id, the count, and one
        machine's nominal CPU, nominal and reported memory, and cards."""
        unit = ComputeUnitTable
        return [
            (str(unit_id), int(stopped), int(cpu), int(memory), int(reported), int(gpu))
            for unit_id, stopped, cpu, memory, reported, gpu in self.session.execute(
                select(
                    unit.id,
                    unit.stopped_machines,
                    unit.worker_cpu_millicores,
                    unit.worker_memory_mib,
                    _reported_memory(),
                    unit.worker_gpu_count,
                )
                .outerjoin(ComputeNodeShapeTable, _same_shape(unit))
                .where(
                    unit.visibility == ComputeUnitVisibility.Internal.value,
                    unit.platform_fleet.is_(True),
                    unit.worker_preemptible.is_(preemptible),
                    unit.worker_gpu_type == gpu_type,
                    unit.stopped_machines > 0,
                )
            ).tuples()
        ]

    def stopped_reserve_units(self) -> list[StoppedReserveUnitRow]:
        """Platform units holding stopped reserves, and whether one can resume now."""
        unit = ComputeUnitTable
        resumable = exists().where(
            ComputeProviderInstanceTable.pool_id == unit.id,
            ComputeProviderInstanceTable.status == "stopped",
            ComputeProviderInstanceTable.missing_since.is_(None),
        )
        return [
            StoppedReserveUnitRow(
                id=str(unit_id),
                preemptible=preemptible,
                gpu_type=gpu_type,
                cpu_millicores=cpu,
                memory_mib=memory,
                reported_memory_mib=reported,
                gpu_count=gpu,
                stopped=stopped,
                resumable=bool(ready),
            )
            for unit_id, preemptible, gpu_type, cpu, memory, reported, gpu, stopped, ready in (
                self.session.execute(
                    select(
                        unit.id,
                        unit.worker_preemptible,
                        unit.worker_gpu_type,
                        unit.worker_cpu_millicores,
                        unit.worker_memory_mib,
                        _reported_memory(),
                        unit.worker_gpu_count,
                        unit.stopped_machines,
                        resumable,
                    )
                    .outerjoin(ComputeNodeShapeTable, _same_shape(unit))
                    .where(
                        unit.platform_fleet.is_(True),
                        unit.visibility == ComputeUnitVisibility.Internal.value,
                        unit.phase.not_in(
                            (ComputeUnitPhase.Deleting.value, ComputeUnitPhase.Deleted.value)
                        ),
                        or_(unit.stopped_machines > 0, resumable),
                    )
                ).tuples()
            )
        ]

    def offer_states(
        self,
        identities: Collection[tuple[str, str, str, str, int]],
    ) -> dict[tuple[str, str, str, str, int], ComputeOfferState]:
        if not identities:
            return {}
        table = ComputeUnitTable
        rows = self.session.execute(
            select(
                table.workspace_id,
                table.provider_ref,
                table.region,
                table.capability_key,
                table.root_volume_gib,
                table.id,
                table.desired_machines,
                table.observed_machines,
                table.phase,
                table.degraded_reason,
                table.degraded_at,
                table.last_capacity_failure_at,
                table.registration_timeout_seconds,
            ).where(
                tuple_(
                    table.workspace_id,
                    table.provider_ref,
                    table.region,
                    table.capability_key,
                    table.root_volume_gib,
                ).in_(identities)
            )
        ).tuples()
        return {
            (workspace, provider, region, capability, volume): ComputeOfferState(
                id=identity,
                desired_machines=desired,
                observed_machines=observed,
                phase=ComputeUnitPhase(phase),
                registration_timeout_seconds=timeout,
                provider_state=ComputeUnitProviderState(
                    degraded_reason=reason,
                    degraded_at=to_utc_or_none(degraded_at),
                    last_capacity_failure_at=to_utc_or_none(failed_at),
                ),
            )
            for (
                workspace,
                provider,
                region,
                capability,
                volume,
                identity,
                desired,
                observed,
                phase,
                reason,
                degraded_at,
                failed_at,
                timeout,
            ) in rows
        }

    def upsert(self, record: ComputeUnitRecord) -> ComputeUnitRecord:
        # The immutability comparison below is by identity, and it runs before the
        # store's own validation, so the record has to be typed by the time it
        # gets there or an unchanged owner reads as a changed one. `dict(record)`
        # rather than `model_dump`: dumping serializes, and a drifted record would
        # raise the serializer warning here instead of where it was introduced.
        record = ComputeUnitRecord.model_validate(dict(record))
        WorkspaceRepository(self.session).lock_active_owner(record.workspace_id)
        current = self.get(record.id, for_update=True)
        if current is not None:
            # The row owns its creation time; a caller rebuilding the record
            # from scratch must not be able to move it.
            record = record.model_copy(update={"created_at": current.created_at})
        if current is not None and (
            current.workspace_id != record.workspace_id
            or current.capacity_owner_id != record.capacity_owner_id
            or current.capacity_owner_kind is not record.capacity_owner_kind
            or current.capacity_owner_source is not record.capacity_owner_source
        ):
            raise ConflictError(f"compute unit capacity owner is immutable: {record.id}")
        row = self.session.get(ComputeUnitTable, record.id)
        if row is None:
            row = ComputeUnitTable(id=record.id, created_at=record.created_at)
            self.session.add(row)
        row.workspace_id = record.workspace_id
        row.name = record.name
        row.status = record.status
        row.selector = record.selector
        row.source = record.source
        row.expires_at = record.expires_at
        row.updated_at = utc_now()
        self._write_columns(row, record)
        return _compute_unit_record(row)

    def delete(self, pool_id: str, *, workspace_id: str) -> None:
        self.session.execute(
            delete(ComputeUnitTable).where(
                ComputeUnitTable.id == pool_id,
                ComputeUnitTable.workspace_id == workspace_id,
            )
        )
        self.session.flush()

    def get_by_name(
        self,
        workspace_id: str,
        name: str,
        *,
        for_update: bool = False,
    ) -> ComputeUnitRecord | None:
        """Resolve a unit by name, which only a create path may do.

        A name is this table's creation-time natural key and nothing else.
        Runtime callers address a unit by `id`/`capacity_owner_id`; a name is a
        free-form string and keying on it lets the wrong one be passed.
        """
        statement = select(ComputeUnitTable).where(
            ComputeUnitTable.workspace_id == workspace_id,
            ComputeUnitTable.name == name,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).one_or_none()
        return _compute_unit_record(row) if row is not None else None

    def get_by_capacity_owner_id(
        self,
        capacity_owner_id: str,
        *,
        workspace_id: str | None = None,
        for_update: bool = False,
    ) -> ComputeUnitRecord | None:
        """Resolve one unit by its owner.

        System callers may omit workspace scope for a globally unique owner.
        Tenant callers supply it so the predicate enforces the boundary.
        """
        statement = select(ComputeUnitTable).where(
            ComputeUnitTable.capacity_owner_id == capacity_owner_id
        )
        if workspace_id is not None:
            statement = statement.where(ComputeUnitTable.workspace_id == workspace_id)
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).one_or_none()
        return _compute_unit_record(row) if row is not None else None

    def sizing_for_owner(self, capacity_owner_id: str) -> ComputeUnitSizingRecord | None:
        row = self.session.execute(
            select(
                ComputeUnitTable.id,
                ComputeUnitTable.capacity_owner_kind,
                ComputeUnitTable.desired_machines,
            ).where(ComputeUnitTable.capacity_owner_id == capacity_owner_id)
        ).one_or_none()
        if row is None:
            return None
        return ComputeUnitSizingRecord(
            id=row.id,
            capacity_owner_kind=CapacityOwnerKind(row.capacity_owner_kind),
            desired_machines=row.desired_machines,
        )

    def delete_for_workspace_deletion(self, pool_id: str, *, workspace_id: str) -> bool:
        workspace = WorkspaceRepository(self.session).lock_for_deletion(workspace_id)
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
        result = self.session.execute(
            delete(ComputeUnitTable).where(
                ComputeUnitTable.id == pool_id,
                ComputeUnitTable.workspace_id == workspace_id,
            )
        )
        self.session.flush()
        return isinstance(result, CursorResult) and result.rowcount > 0

    def get(
        self, pool_id: str, *, workspace_id: str | None = None, for_update: bool = False
    ) -> ComputeUnitRecord | None:
        statement = select(ComputeUnitTable).where(ComputeUnitTable.id == pool_id)
        if workspace_id is not None:
            statement = statement.where(ComputeUnitTable.workspace_id == workspace_id)
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return _compute_unit_record(row) if row is not None else None

    def get_by_identity(
        self,
        *,
        workspace_id: str,
        provider_ref: str,
        region: str,
        capability_key: str,
        root_volume_gib: int,
        for_update: bool = False,
    ) -> ComputeUnitRecord | None:
        """Look up a provisioning unit by everything AWS pins to one ASG.

        `root_volume_gib` belongs to the identity because it feeds the launch
        template: two callers disagreeing on it for one capability key would
        alternate the template version on every reconcile and no node would
        ever settle.
        """
        statement = select(ComputeUnitTable).where(
            ComputeUnitTable.workspace_id == workspace_id,
            ComputeUnitTable.provider_ref == provider_ref,
            ComputeUnitTable.region == region,
            ComputeUnitTable.capability_key == capability_key,
            ComputeUnitTable.root_volume_gib == root_volume_gib,
            ComputeUnitTable.visibility == ComputeUnitVisibility.Internal.value,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return _compute_unit_record(row) if row is not None else None

    def list_for_workspace(
        self, workspace_id: str, *, include_retired_platform: bool = True
    ) -> list[ComputeUnitRecord]:
        statement = (
            select(ComputeUnitTable)
            .where(ComputeUnitTable.workspace_id == workspace_id)
            .order_by(ComputeUnitTable.created_at, ComputeUnitTable.id)
        )
        if not include_retired_platform:
            statement = statement.where(
                ~and_(
                    ComputeUnitTable.platform_fleet.is_(True),
                    ComputeUnitTable.visibility == ComputeUnitVisibility.Internal.value,
                    ComputeUnitTable.phase == ComputeUnitPhase.Deleted.value,
                )
            )
        return [_compute_unit_record(row) for row in self.session.scalars(statement)]

    def list_for_account(self, user_id: str) -> list[ComputeUnitRecord]:
        statement = (
            select(ComputeUnitTable)
            .where(
                or_(
                    and_(
                        ComputeUnitTable.provider == "local",
                        exists().where(
                            WorkspaceMemberTable.workspace_id == ComputeUnitTable.workspace_id,
                            WorkspaceMemberTable.user_id == user_id,
                            WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
                        ),
                    ),
                    exists().where(
                        ComputeMachineEnrollmentTable.capacity_owner_id == ComputeUnitTable.id,
                        ComputeMachineEnrollmentTable.user_id == user_id,
                    ),
                    exists().where(
                        ComputeJoinCredentialTable.capacity_owner_id == ComputeUnitTable.id,
                        ComputeJoinCredentialTable.user_id == user_id,
                        ComputeJoinCredentialTable.status == ComputeCredentialStatus.Active.value,
                        ComputeJoinCredentialTable.expires_at > utc_now(),
                        ComputeJoinCredentialTable.use_count < ComputeJoinCredentialTable.max_uses,
                    ),
                    exists().where(
                        AwsAccountConnectionTable.id == ComputeUnitTable.provider_connection_id,
                        AwsAccountConnectionTable.user_id == user_id,
                    ),
                )
            )
            .order_by(ComputeUnitTable.created_at, ComputeUnitTable.id)
        )
        return [_compute_unit_record(row) for row in self.session.scalars(statement)]

    def empty_joined_units(self) -> list[ComputeUnitRecord]:
        statement = select(ComputeUnitTable).where(
            ComputeUnitTable.provider == "agent",
            exists().where(
                ComputeJoinCredentialTable.capacity_owner_id == ComputeUnitTable.id,
            ),
            ~exists().where(
                ComputeMachineEnrollmentTable.capacity_owner_id == ComputeUnitTable.id,
            ),
        )
        return [_compute_unit_record(row) for row in self.session.scalars(statement)]

    def list_across_workspaces(
        self, *, capacity_owner_kind: CapacityOwnerKind | None = None
    ) -> list[ComputeUnitRecord]:
        """Current units for scheduler controller construction."""
        statement = (
            select(ComputeUnitTable)
            .where(ComputeUnitTable.phase != ComputeUnitPhase.Deleted.value)
            .order_by(ComputeUnitTable.workspace_id, ComputeUnitTable.id)
        )
        if capacity_owner_kind is not None:
            statement = statement.where(
                ComputeUnitTable.capacity_owner_kind == capacity_owner_kind.value
            )
        return [_compute_unit_record(row) for row in self.session.scalars(statement)]

    def claim_reconciliation_batch(
        self,
        kind: ComputeReconciliationKind,
        *,
        now: datetime,
        limit: int,
    ) -> list[ComputeUnitRecord]:
        if limit < 1:
            return []
        attempted_at = (
            ComputeUnitTable.provider_reconcile_attempt_at
            if kind is ComputeReconciliationKind.Provider
            else ComputeUnitTable.drain_reconcile_attempt_at
        )
        active = or_(
            ComputeUnitTable.desired_machines > 0,
            ComputeUnitTable.stopped_machines > 0,
            ComputeUnitTable.retiring_stopped_machines > 0,
            ComputeUnitTable.observed_machines > 0,
            ComputeUnitTable.provider_committed_machines > 0,
            ComputeUnitTable.phase == ComputeUnitPhase.Deleting.value,
            exists().where(
                ComputeProviderInstanceTable.pool_id == ComputeUnitTable.id,
                ComputeProviderInstanceTable.status.not_in(("deleted", "failed")),
            ),
        )
        base = (
            select(ComputeUnitTable)
            .where(
                ComputeUnitTable.visibility == ComputeUnitVisibility.Internal.value,
                ComputeUnitTable.phase != ComputeUnitPhase.Deleted.value,
            )
            .order_by(attempted_at.asc().nulls_first(), ComputeUnitTable.id)
            .with_for_update(skip_locked=True)
        )
        # Reserve an audit slot for empty pools so stale provider resources are
        # eventually found even when active capacity always needs attention.
        rows = list(self.session.scalars(base.where(active).limit(max(limit - 1, 1))))
        if len(rows) < limit:
            rows.extend(self.session.scalars(base.where(~active).limit(limit - len(rows))))
        if rows:
            stamp = update(ComputeUnitTable).where(
                ComputeUnitTable.id.in_([row.id for row in rows])
            )
            # Attempt bookkeeping must not reorder units for capacity selection.
            stamp = (
                stamp.values(
                    provider_reconcile_attempt_at=now, updated_at=ComputeUnitTable.updated_at
                )
                if kind is ComputeReconciliationKind.Provider
                else stamp.values(
                    drain_reconcile_attempt_at=now, updated_at=ComputeUnitTable.updated_at
                )
            )
            self.session.execute(stamp)
        self.session.flush()
        return [_compute_unit_record(row) for row in rows]

    def list_platform_internal(
        self, *, preemptible: bool | None = None, gpu: bool | None = None
    ) -> list[ComputeUnitRecord]:
        statement = select(ComputeUnitTable).where(
            ComputeUnitTable.visibility == ComputeUnitVisibility.Internal.value,
            ComputeUnitTable.platform_fleet.is_(True),
            ComputeUnitTable.phase != ComputeUnitPhase.Deleted.value,
        )
        if preemptible is not None:
            statement = statement.where(ComputeUnitTable.worker_preemptible.is_(preemptible))
        if gpu is not None:
            statement = statement.where(
                ComputeUnitTable.worker_gpu_count > 0
                if gpu
                else ComputeUnitTable.worker_gpu_count == 0
            )
        statement = statement.order_by(ComputeUnitTable.updated_at, ComputeUnitTable.id)
        return [_compute_unit_record(row) for row in self.session.scalars(statement)]

    def list_internal(self, *, workspace_id: str) -> list[ComputeUnitRecord]:
        statement = select(ComputeUnitTable).where(
            ComputeUnitTable.visibility == ComputeUnitVisibility.Internal.value,
            ComputeUnitTable.workspace_id == workspace_id,
        )
        statement = statement.order_by(ComputeUnitTable.updated_at, ComputeUnitTable.id)
        return [_compute_unit_record(row) for row in self.session.scalars(statement)]

    def list_for_provider_connection(self, connection_id: str) -> list[ComputeUnitRecord]:
        statement = (
            select(ComputeUnitTable)
            .where(ComputeUnitTable.provider_connection_id == connection_id)
            .order_by(ComputeUnitTable.created_at, ComputeUnitTable.id)
        )
        return [_compute_unit_record(row) for row in self.session.scalars(statement)]

    def lock_capacity_workspace(self, workspace_id: str) -> None:
        workspace = self.session.scalar(
            select(WorkspaceTable.id).where(WorkspaceTable.id == workspace_id).with_for_update()
        )
        if workspace is None:
            raise LookupError("provider capacity workspace no longer exists")

    def lock_platform_capacity(self) -> None:
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": "compute:platform-capacity"},
        )

    def platform_capacity_usage(self, *, gpu: bool, excluding_unit_id: str | None = None) -> int:
        statement = self._platform_capacity_commitments(gpu=gpu)
        if excluding_unit_id is not None:
            statement = statement.where(ComputeUnitTable.id != excluding_unit_id)
        commitments = statement.subquery()
        return int(
            self.session.scalar(select(func.coalesce(func.sum(commitments.c.committed), 0))) or 0
        )

    def platform_capacity_by_unit(self, *, gpu: bool) -> dict[str, int]:
        return {
            str(unit_id): committed
            for unit_id, committed in self.session.execute(
                self._platform_capacity_commitments(gpu=gpu)
            ).tuples()
        }

    @staticmethod
    def _platform_capacity_commitments(*, gpu: bool) -> Select[tuple[str, int]]:
        # Retiring nodes still bill after desired capacity is reduced or replaced.
        # An explicitly paired replacement already occupies the unit's surge slot.
        live_instances = (
            select(
                ComputeProviderInstanceTable.pool_id,
                func.count().label("count"),
                func.count()
                .filter(
                    ComputeProviderInstanceTable.status == "terminating",
                    or_(
                        ComputeProviderInstanceTable.machine_id.is_(None),
                        cast(ComputeProviderInstanceTable.machine_id, String)
                        != func.coalesce(ComputeUnitTable.replacement_machine_id, ""),
                    ),
                )
                .label("retiring_count"),
            )
            .join(ComputeUnitTable, ComputeUnitTable.id == ComputeProviderInstanceTable.pool_id)
            .where(ComputeProviderInstanceTable.status.not_in(("deleted", "failed")))
            .group_by(ComputeProviderInstanceTable.pool_id)
            .subquery()
        )
        surge = case(
            (
                func.coalesce(ComputeUnitTable.replacement_machine_id, "") != "",
                1,
            ),
            else_=0,
        )
        return (
            select(
                ComputeUnitTable.id,
                func.greatest(
                    ComputeUnitTable.desired_machines
                    + ComputeUnitTable.stopped_machines
                    + ComputeUnitTable.retiring_stopped_machines
                    + surge
                    + func.coalesce(live_instances.c.retiring_count, 0),
                    ComputeUnitTable.observed_machines,
                    func.coalesce(live_instances.c.count, 0),
                    ComputeUnitTable.provider_committed_machines,
                ).label("committed"),
            )
            .outerjoin(live_instances, live_instances.c.pool_id == ComputeUnitTable.id)
            .where(
                ComputeUnitTable.visibility == ComputeUnitVisibility.Internal.value,
                ComputeUnitTable.platform_fleet.is_(True),
                or_(
                    ComputeUnitTable.desired_machines > 0,
                    ComputeUnitTable.stopped_machines > 0,
                    ComputeUnitTable.retiring_stopped_machines > 0,
                    ComputeUnitTable.observed_machines > 0,
                    ComputeUnitTable.provider_committed_machines > 0,
                    live_instances.c.count > 0,
                    surge > 0,
                ),
                (
                    ComputeUnitTable.worker_gpu_count > 0
                    if gpu
                    else ComputeUnitTable.worker_gpu_count == 0
                ),
            )
        )

    def update_capacity(
        self,
        pool_id: str,
        *,
        expected_generation: int,
        desired_machines: int,
        max_machines: int,
        observed_machines: int,
        phase: ComputeUnitPhase,
        provider_state: ComputeUnitProviderState,
        replacement_machine_id: str | None = None,
        replacement_template_version: str | None = None,
        stopped_machines: int | None = None,
    ) -> ComputeUnitRecord | None:
        current = self.get(pool_id, for_update=True)
        if current is None or current.generation != expected_generation:
            return None
        stopped = current.stopped_machines if stopped_machines is None else stopped_machines
        updated = current.model_copy(
            update={
                "desired_machines": desired_machines,
                "stopped_machines": max(min(stopped, max_machines - desired_machines), 0),
                "max_machines": max_machines,
                "observed_machines": observed_machines,
                "generation": current.generation + 1,
                "phase": phase,
                "status": phase.value,
                "provider_state": provider_state,
                "replacement_machine_id": (
                    current.replacement_machine_id
                    if replacement_machine_id is None
                    else replacement_machine_id
                ),
                "replacement_template_version": (
                    current.replacement_template_version
                    if replacement_template_version is None
                    else replacement_template_version
                ),
            }
        )
        return self.upsert(updated)

    def provider_checkpoint(
        self, pool_id: str, *, workspace_id: str, provider_ref: str, generation: int
    ) -> ComputeUnitProviderState | None:
        row = self.session.execute(
            select(
                ComputeUnitTable.provider_resource_id,
                ComputeUnitTable.provider_attributes,
                ComputeUnitTable.provider_state_revision,
                ComputeUnitTable.provider_committed_machines,
            ).where(
                ComputeUnitTable.id == pool_id,
                ComputeUnitTable.workspace_id == workspace_id,
                ComputeUnitTable.provider_ref == provider_ref,
                ComputeUnitTable.generation == generation,
            )
        ).one_or_none()
        if row is None:
            return None
        return ComputeUnitProviderState(
            resource_id=row[0], attributes=row[1], revision=row[2], committed_machines=row[3]
        )

    def checkpoint_provider_state(
        self,
        pool_id: str,
        *,
        workspace_id: str,
        provider_ref: str,
        generation: int,
        expected: ComputeUnitProviderState,
        state: ComputeUnitProviderState,
    ) -> bool:
        statement = (
            update(ComputeUnitTable)
            .where(
                ComputeUnitTable.id == pool_id,
                ComputeUnitTable.workspace_id == workspace_id,
                ComputeUnitTable.provider_ref == provider_ref,
                ComputeUnitTable.generation == generation,
                ComputeUnitTable.provider_resource_id == expected.resource_id,
                ComputeUnitTable.provider_attributes == dict(expected.attributes),
                ComputeUnitTable.provider_state_revision == expected.revision,
                ComputeUnitTable.provider_committed_machines == expected.committed_machines,
            )
            .values(
                provider_resource_id=state.resource_id,
                provider_attributes=dict(state.attributes),
                provider_state_revision=state.revision,
                provider_committed_machines=state.committed_machines,
            )
            .returning(ComputeUnitTable.id)
        )
        return self.session.scalar(statement) is not None

    def apply_provider_state(
        self,
        pool_id: str,
        *,
        generation: int,
        observed_machines: int,
        phase: ComputeUnitPhase,
        provider_state: ComputeUnitProviderState,
    ) -> ComputeUnitRecord | None:
        current = self.get(pool_id, for_update=True)
        if (
            current is None
            or current.generation != generation
            or provider_state.revision != current.provider_state.revision
        ):
            return None
        updated = current.model_copy(
            update={
                "observed_machines": observed_machines,
                "phase": phase,
                "status": phase.value,
                "provider_state": provider_state,
            }
        )
        return self.upsert(updated)

    def _write_columns(self, row: ComputeUnitTable, record: ComputeUnitRecord) -> None:
        if (
            row.provider_state_revision is not None
            and row.provider_state_revision > record.provider_state.revision
        ):
            raise ConflictError("provider operation state changed during capacity mutation")
        row.provider_ref = record.provider_ref
        row.capacity_owner_id = record.capacity_owner_id
        row.capacity_owner_kind = record.capacity_owner_kind.value
        row.capacity_owner_source = record.capacity_owner_source.value
        row.provider_connection_id = record.provider_connection_id
        row.capacity_mode = record.capacity_mode.value
        row.visibility = record.visibility.value
        row.region = record.region
        row.offer_id = record.offer_id
        row.capability_key = record.capability_key
        row.placement = record.placement.key
        row.provider = record.provider
        row.desired_machines = record.desired_machines
        row.stopped_machines = record.stopped_machines
        row.retiring_stopped_machines = record.retiring_stopped_machines
        row.initial_machines = record.initial_machines
        row.min_machines = record.min_machines
        row.max_machines = record.max_machines
        row.observed_machines = record.observed_machines
        row.generation = record.generation
        row.phase = record.phase.value
        row.provider_resource_id = record.provider_state.resource_id
        row.provider_state_revision = record.provider_state.revision
        row.provider_committed_machines = record.provider_state.committed_machines
        row.provider_attributes = dict(record.provider_state.attributes)
        row.degraded_reason = record.provider_state.degraded_reason
        row.degraded_at = record.provider_state.degraded_at
        row.last_capacity_failure_at = record.provider_state.last_capacity_failure_at
        row.launch_attempt_baseline = record.provider_state.launch_attempt_baseline
        row.platform_fleet = record.platform_fleet
        row.offer_cost_terms = (
            record.offer_cost_terms.model_dump(mode="json")
            if record.offer_cost_terms is not None
            else None
        )
        row.offer_storage_mib = record.offer_storage_mib
        row.offer_availability_zone = record.offer_availability_zone
        row.supplier_cpu_unit = record.supplier_cpu_unit.value
        row.supplier_cpu_count = record.supplier_cpu_count
        row.replacement_machine_id = record.replacement_machine_id
        row.replacement_template_version = record.replacement_template_version
        row.scaling_enabled = record.scaling_enabled
        row.priority = record.priority
        row.min_free_cpu_millicores = record.min_free_cpu_millicores
        row.min_free_memory_mib = record.min_free_memory_mib
        row.min_free_gpu_count = record.min_free_gpu_count
        row.worker_cpu_millicores = record.worker_cpu_millicores
        row.worker_memory_mib = record.worker_memory_mib
        row.worker_gpu_type = record.worker_gpu_type
        row.worker_gpu_count = record.worker_gpu_count
        row.worker_runtimes = list(record.worker_runtimes)
        row.worker_preemptible = record.worker_preemptible
        row.idle_drain_timeout_seconds = record.idle_drain_timeout_seconds
        row.scale_up_cooldown_seconds = record.scale_up_cooldown_seconds
        row.scale_down_cooldown_seconds = record.scale_down_cooldown_seconds
        row.registration_timeout_seconds = record.registration_timeout_seconds
        row.root_volume_gib = record.root_volume_gib
        row.fallback = record.fallback.value
        self.session.flush()


def _capacity_operation_record(
    row: ComputeCapacityOperationTable,
) -> ComputeCapacityOperationRecord:
    return ComputeCapacityOperationRecord.model_validate(
        {
            "id": row.id,
            "workspace_id": row.workspace_id,
            "pool_id": row.pool_id,
            "capacity_owner_id": row.capacity_owner_id,
            "reservation_id": row.reservation_id,
            "operation_id": row.operation_id,
            "demand_container_id": row.demand_container_id,
            "desired_unit": row.desired_unit,
            "status": row.status,
            "target_machine_id": row.target_machine_id,
            "fulfilled_at": to_utc_or_none(row.fulfilled_at),
            "provider_instance_id": row.provider_instance_id,
            "previous_desired_unit": row.previous_desired_unit,
            "release_desired_unit": row.release_desired_unit,
            "owns_capacity": row.owns_capacity,
            "join_attempt": row.join_attempt,
            "shape": CapacityAcquisitionShape(
                cpu_millicores=row.cpu_millicores,
                memory_mib=row.memory_mib,
                gpu_type=row.gpu_type,
                gpu_count=row.gpu_count,
                runtime=row.runtime,
                preemptible=row.preemptible,
            ),
            "failure_code": row.failure_code,
            "failure_count": row.failure_count,
            "last_error": row.last_error,
            "created_at": to_utc(row.created_at),
            "updated_at": to_utc(row.updated_at),
        }
    )


@dataclass(slots=True)
class ComputeCapacityOperationRepository:
    session: Session

    def latest_failures_for_containers(
        self, container_ids: Collection[str]
    ) -> dict[str, CapacityFailureCode]:
        if not container_ids:
            return {}
        table = ComputeCapacityOperationTable
        failures = (
            select(
                table.demand_container_id.label("container_id"),
                table.failure_code.label("failure_code"),
                func.row_number()
                .over(
                    partition_by=table.demand_container_id,
                    order_by=(table.created_at.desc(), table.id.desc()),
                )
                .label("rank"),
            )
            .where(
                table.demand_container_id.in_(container_ids),
                table.failure_code.is_not(None),
            )
            .subquery()
        )
        rows = self.session.execute(
            select(failures.c.container_id, failures.c.failure_code).where(failures.c.rank == 1)
        ).tuples()
        return {container_id: CapacityFailureCode(code) for container_id, code in rows}

    def get(
        self,
        capacity_owner_id: str,
        operation_id: str,
        *,
        for_update: bool = False,
    ) -> ComputeCapacityOperationRecord | None:
        statement = select(ComputeCapacityOperationTable).where(
            ComputeCapacityOperationTable.capacity_owner_id == capacity_owner_id,
            ComputeCapacityOperationTable.operation_id == operation_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).one_or_none()
        return _capacity_operation_record(row) if row is not None else None

    def get_by_reservation(
        self,
        reservation_id: str,
        *,
        for_update: bool = False,
    ) -> ComputeCapacityOperationRecord | None:
        statement = select(ComputeCapacityOperationTable).where(
            ComputeCapacityOperationTable.reservation_id == reservation_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).one_or_none()
        return _capacity_operation_record(row) if row is not None else None

    def list_open_for_owner(self, capacity_owner_id: str) -> list[ComputeCapacityOperationRecord]:
        rows = self.session.scalars(
            select(ComputeCapacityOperationTable)
            .where(
                ComputeCapacityOperationTable.capacity_owner_id == capacity_owner_id,
                ComputeCapacityOperationTable.status.not_in(
                    tuple(status.value for status in CapacityOperationStatus if status.terminal)
                ),
                ComputeCapacityOperationTable.owns_capacity.is_(True),
            )
            .order_by(ComputeCapacityOperationTable.created_at, ComputeCapacityOperationTable.id)
        )
        return [_capacity_operation_record(row) for row in rows]

    def list_open_sizing_for_owner(
        self,
        capacity_owner_id: str,
    ) -> list[ComputeCapacityOperationSizingRecord]:
        rows = self.session.execute(
            select(
                ComputeCapacityOperationTable.operation_id,
                ComputeCapacityOperationTable.desired_unit,
                ComputeCapacityOperationTable.status,
                func.coalesce(
                    ComputeCapacityOperationTable.owns_capacity,
                    False,
                ),
                func.coalesce(
                    ComputeCapacityOperationTable.failure_count,
                    0,
                ),
                ComputeCapacityOperationTable.updated_at,
            )
            .where(
                ComputeCapacityOperationTable.capacity_owner_id == capacity_owner_id,
                ComputeCapacityOperationTable.status.not_in(
                    tuple(status.value for status in CapacityOperationStatus if status.terminal)
                ),
                ComputeCapacityOperationTable.owns_capacity.is_(True),
            )
            .order_by(
                ComputeCapacityOperationTable.created_at,
                ComputeCapacityOperationTable.id,
            )
        ).tuples()
        return [
            ComputeCapacityOperationSizingRecord(
                operation_id=operation_id,
                desired_unit=desired_unit,
                status=CapacityOperationStatus(status),
                owns_capacity=owns_capacity,
                failure_count=failure_count,
                updated_at=to_utc(updated_at),
            )
            for (
                operation_id,
                desired_unit,
                status,
                owns_capacity,
                failure_count,
                updated_at,
            ) in rows
        ]

    def expired_for_owner(
        self, capacity_owner_id: str, *, created_before: datetime
    ) -> list[ComputeCapacityOperationRecord]:
        rows = self.session.scalars(
            select(ComputeCapacityOperationTable)
            .where(
                ComputeCapacityOperationTable.capacity_owner_id == capacity_owner_id,
                ComputeCapacityOperationTable.created_at <= created_before,
                ComputeCapacityOperationTable.owns_capacity.is_(True),
                ComputeCapacityOperationTable.status.in_(
                    tuple(status.value for status in CapacityOperationStatus if not status.terminal)
                ),
            )
            .order_by(ComputeCapacityOperationTable.created_at, ComputeCapacityOperationTable.id)
        )
        return [_capacity_operation_record(row) for row in rows]

    def sizing_history_summary_for_owner(
        self,
        capacity_owner_id: str,
    ) -> ComputeCapacityOperationHistorySummary:
        """Summarize the retained request history.

        Released operations stay in the aggregate so the peak remains monotonic.
        That stops the sizer from buying back every machine the drain controller
        retires.
        """
        peak_desired_unit, last_requested_at = self.session.execute(
            select(
                func.max(ComputeCapacityOperationTable.desired_unit),
                func.max(ComputeCapacityOperationTable.created_at),
            ).where(ComputeCapacityOperationTable.capacity_owner_id == capacity_owner_id)
        ).one()
        return ComputeCapacityOperationHistorySummary(
            peak_desired_unit=int(peak_desired_unit or 0),
            last_requested_at=(
                to_utc(last_requested_at) if last_requested_at is not None else None
            ),
        )

    def upsert(self, record: ComputeCapacityOperationRecord) -> ComputeCapacityOperationRecord:
        record = ComputeCapacityOperationRecord.model_validate(record.model_dump())
        WorkspaceRepository(self.session).lock_active_owner(record.workspace_id)
        current = self.get(record.capacity_owner_id, record.operation_id, for_update=True)
        if (
            current is not None
            and current.status.terminal
            and (
                current.status is not record.status
                or current.target_machine_id != record.target_machine_id
                or (not current.owns_capacity and record.owns_capacity)
            )
        ):
            raise ConflictError("terminal capacity ownership cannot be reopened")
        if current is not None and (
            current.reservation_id != record.reservation_id
            or current.pool_id != record.pool_id
            or current.workspace_id != record.workspace_id
            or current.desired_unit != record.desired_unit
            or current.demand_container_id != record.demand_container_id
            or current.shape != record.shape
        ):
            raise ConflictError(
                f"capacity operation identity or request is immutable: {record.operation_id}"
            )
        reservation = self.get_by_reservation(record.reservation_id, for_update=True)
        if reservation is not None and reservation.operation_id != record.operation_id:
            raise ConflictError(
                f"capacity reservation is already owned by another operation: "
                f"{record.reservation_id}"
            )
        row = self.session.get(ComputeCapacityOperationTable, record.id)
        if row is None:
            row = ComputeCapacityOperationTable(id=record.id, created_at=record.created_at)
            self.session.add(row)
        row.workspace_id = record.workspace_id
        row.pool_id = record.pool_id
        row.capacity_owner_id = record.capacity_owner_id
        row.reservation_id = record.reservation_id
        row.operation_id = record.operation_id
        row.desired_unit = record.desired_unit
        row.status = record.status.value
        row.target_machine_id = record.target_machine_id
        row.demand_container_id = record.demand_container_id
        row.fulfilled_at = record.fulfilled_at
        row.provider_instance_id = record.provider_instance_id
        row.previous_desired_unit = record.previous_desired_unit
        row.release_desired_unit = record.release_desired_unit
        row.owns_capacity = record.owns_capacity
        row.join_attempt = record.join_attempt
        row.failure_code = record.failure_code.value if record.failure_code is not None else None
        row.failure_count = record.failure_count
        row.last_error = record.last_error
        row.cpu_millicores = record.shape.cpu_millicores
        row.memory_mib = record.shape.memory_mib
        row.gpu_type = record.shape.gpu_type
        row.gpu_count = record.shape.gpu_count
        row.runtime = record.shape.runtime
        row.preemptible = record.shape.preemptible
        row.updated_at = record.updated_at
        self.session.flush()
        return _capacity_operation_record(row)


def _provider_instance_record(row: ComputeProviderInstanceTable) -> ComputeProviderInstanceRecord:
    return ComputeProviderInstanceRecord.model_validate(
        {
            "id": row.id,
            "provider": row.provider,
            "offer_id": row.offer_id,
            "status": row.status,
            "source": row.source,
            "pool_id": row.pool_id,
            "instance_type": row.instance_type,
            "instance_id": row.instance_id,
            "machine_id": row.machine_id,
            "gpu": row.gpu,
            "gpu_count": row.gpu_count,
            "cpu_millicores": row.cpu_millicores,
            "memory_mb": row.memory_mb,
            "cost_terms": SupplierCostTerms.model_validate(row.cost_terms),
            "storage_mib": row.storage_mib,
            "supplier_cpu_unit": row.supplier_cpu_unit,
            "supplier_cpu_count": row.supplier_cpu_count,
            "committed_micros": row.committed_micros,
            "expires_at": to_utc_or_none(row.expires_at),
            "billing_renewal_at": to_utc_or_none(row.billing_renewal_at),
            "billing_started_at": to_utc_or_none(row.billing_started_at),
            "first_enrolled_at": to_utc_or_none(row.first_enrolled_at),
            "first_served_at": to_utc_or_none(row.first_served_at),
            "last_served_at": to_utc_or_none(row.last_served_at),
            "unserved_observations": row.unserved_observations,
            "launch_attempt": row.launch_attempt,
            "architecture": row.architecture,
            "runtime": row.runtime,
            "region": row.region,
            "availability_zone": row.availability_zone,
            "storage_volume_ids": row.storage_volume_ids,
            "booted_template_version": row.booted_template_version,
            "prepared_agent_sha256": row.prepared_agent_sha256,
            "prepared_worker_image": row.prepared_worker_image,
            "missing_since": to_utc_or_none(row.missing_since),
            "provider_storage_destroyed_at": to_utc_or_none(row.provider_storage_destroyed_at),
            "terminating_reason": row.terminating_reason,
            "terminated_reason": row.terminated_reason,
            "status_message": row.status_message,
            "last_error": row.last_error,
            "created_at": to_utc(row.created_at),
            "updated_at": to_utc(row.updated_at),
        }
    )


@dataclass(slots=True)
class ComputeProviderInstanceRepository:
    session: Session

    def get(self, instance_id: str) -> ComputeProviderInstanceRecord | None:
        row = self.session.get(ComputeProviderInstanceTable, instance_id)
        return _provider_instance_record(row) if row is not None else None

    def upsert(self, record: ComputeProviderInstanceRecord) -> ComputeProviderInstanceRecord:
        record = ComputeProviderInstanceRecord.model_validate(dict(record))
        row = self.session.get(ComputeProviderInstanceTable, record.id)
        if row is None:
            row = ComputeProviderInstanceTable(id=record.id, created_at=record.created_at)
            self.session.add(row)
        elif row.pool_id != record.pool_id or row.provider != record.provider:
            raise ConflictError("provider instance owner cannot change")
        row.provider = record.provider
        row.offer_id = record.offer_id
        row.status = record.status
        row.source = record.source
        row.pool_id = record.pool_id
        row.instance_type = record.instance_type
        row.instance_id = record.instance_id
        row.machine_id = record.machine_id
        row.gpu = record.gpu
        row.gpu_count = record.gpu_count
        row.cpu_millicores = record.cpu_millicores
        row.memory_mb = record.memory_mb
        row.cost_terms = record.cost_terms.model_dump(mode="json")
        row.storage_mib = record.storage_mib
        row.supplier_cpu_unit = record.supplier_cpu_unit.value
        row.supplier_cpu_count = record.supplier_cpu_count
        row.committed_micros = record.committed_micros
        row.expires_at = record.expires_at
        row.billing_renewal_at = record.billing_renewal_at
        row.billing_started_at = record.billing_started_at
        row.first_enrolled_at = record.first_enrolled_at
        row.first_served_at = record.first_served_at
        row.last_served_at = record.last_served_at
        row.unserved_observations = record.unserved_observations
        row.launch_attempt = record.launch_attempt
        row.architecture = record.architecture
        row.runtime = record.runtime
        row.region = record.region
        row.availability_zone = record.availability_zone
        row.storage_volume_ids = list(record.storage_volume_ids)
        row.booted_template_version = record.booted_template_version
        row.prepared_agent_sha256 = record.prepared_agent_sha256
        row.prepared_worker_image = record.prepared_worker_image
        row.missing_since = record.missing_since
        row.provider_storage_destroyed_at = record.provider_storage_destroyed_at
        row.terminating_reason = record.terminating_reason
        row.terminated_reason = record.terminated_reason
        row.status_message = record.status_message
        row.last_error = record.last_error
        row.updated_at = utc_now()
        self.session.flush()
        return _provider_instance_record(row)

    def list_for_pool(
        self,
        pool_id: str,
        *,
        for_update: bool = False,
        status: str | None = None,
        excluded_statuses: Collection[str] = (),
    ) -> list[ComputeProviderInstanceRecord]:
        statement = (
            select(ComputeProviderInstanceTable)
            .where(ComputeProviderInstanceTable.pool_id == pool_id)
            .order_by(
                ComputeProviderInstanceTable.created_at.desc(),
                ComputeProviderInstanceTable.id.asc(),
            )
        )
        if status is not None:
            statement = statement.where(ComputeProviderInstanceTable.status == status)
        if excluded_statuses:
            statement = statement.where(
                ComputeProviderInstanceTable.status.not_in(excluded_statuses)
            )
        if for_update:
            statement = statement.with_for_update()
        return [_provider_instance_record(row) for row in self.session.scalars(statement)]

    def list_for_reconciliation(
        self,
        pool_id: str,
        *,
        terminal_statuses: Collection[str],
        observed_instance_ids: Collection[str],
        for_update: bool = False,
    ) -> list[ComputeProviderInstanceRecord]:
        table = ComputeProviderInstanceTable
        statement = (
            select(table)
            .where(
                table.pool_id == pool_id,
                or_(
                    table.status.not_in(terminal_statuses),
                    table.instance_id.in_(observed_instance_ids),
                    table.missing_since.is_(None),
                    table.provider_storage_destroyed_at.is_(None),
                ),
            )
            .order_by(table.created_at.desc(), table.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        return [_provider_instance_record(row) for row in self.session.scalars(statement)]

    def highest_launch_attempt(self, pool_id: str, *, default: int = 0) -> int:
        highest = self.session.scalar(
            select(func.max(func.coalesce(ComputeProviderInstanceTable.launch_attempt, 1))).where(
                ComputeProviderInstanceTable.pool_id == pool_id
            )
        )
        return highest if highest is not None else default

    def machine_bindings_for_pool(self, pool_id: str) -> dict[str, str]:
        rows = self.session.execute(
            select(
                ComputeProviderInstanceTable.instance_id,
                ComputeProviderInstanceTable.machine_id,
            ).where(
                ComputeProviderInstanceTable.pool_id == pool_id,
                ComputeProviderInstanceTable.instance_id.is_not(None),
                ComputeProviderInstanceTable.machine_id.is_not(None),
            )
        ).tuples()
        return {
            instance_id: machine_id
            for instance_id, machine_id in rows
            if instance_id is not None and machine_id is not None
        }

    def sizing_summary_for_pool(
        self, pool_id: str, *, terminal_statuses: Collection[str]
    ) -> ComputeProviderInstanceSizingSummary:
        open_count, last_released_at = self.session.execute(
            select(
                func.count().filter(ComputeProviderInstanceTable.status.not_in(terminal_statuses)),
                func.max(ComputeProviderInstanceTable.updated_at).filter(
                    ComputeProviderInstanceTable.status.in_(terminal_statuses)
                ),
            ).where(ComputeProviderInstanceTable.pool_id == pool_id)
        ).one()
        return ComputeProviderInstanceSizingSummary(
            open_count=open_count,
            last_released_at=to_utc(last_released_at) if last_released_at is not None else None,
        )

    def bind_machine(
        self,
        pool_id: str,
        provider_instance_id: str,
        machine_id: str,
    ) -> ComputeProviderInstanceRecord | None:
        row = self.session.scalars(
            select(ComputeProviderInstanceTable)
            .where(
                ComputeProviderInstanceTable.pool_id == pool_id,
                ComputeProviderInstanceTable.instance_id == provider_instance_id,
            )
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        current = _provider_instance_record(row)
        if current.machine_id not in {None, machine_id}:
            return None
        if current.machine_id == machine_id:
            return current
        row.machine_id = machine_id
        row.updated_at = utc_now()
        self.session.flush()
        return _provider_instance_record(row)

    def get_for_pool_instance(
        self,
        pool_id: str,
        provider_instance_id: str,
        *,
        for_update: bool = False,
    ) -> ComputeProviderInstanceRecord | None:
        statement = select(ComputeProviderInstanceTable).where(
            ComputeProviderInstanceTable.pool_id == pool_id,
            ComputeProviderInstanceTable.instance_id == provider_instance_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).one_or_none()
        return _provider_instance_record(row) if row is not None else None

    def list_by_machine_ids(
        self, machine_ids: Collection[str]
    ) -> dict[str, ComputeProviderInstanceRecord]:
        """The provider row bound to each of these machines, keyed by machine id."""
        if not machine_ids:
            return {}
        rows = self.session.scalars(
            select(ComputeProviderInstanceTable).where(
                ComputeProviderInstanceTable.machine_id.in_(list(machine_ids))
            )
        )
        return {
            row.machine_id: _provider_instance_record(row)
            for row in rows
            if row.machine_id is not None
        }

    def stale_platform_reserves(
        self, *, agent_sha256: str, worker_image: str
    ) -> list[ComputeReserveInstance]:
        """Stopped platform reserves prepared with anything but this release."""
        table = ComputeProviderInstanceTable
        rows = self.session.execute(
            select(table.pool_id, table.instance_id, table.machine_id)
            .join(ComputeUnitTable, ComputeUnitTable.id == table.pool_id)
            .where(
                table.status == "stopped",
                table.missing_since.is_(None),
                table.instance_id.is_not(None),
                table.machine_id.is_not(None),
                or_(
                    table.prepared_agent_sha256 != agent_sha256,
                    table.prepared_worker_image != worker_image,
                ),
                ComputeUnitTable.platform_fleet.is_(True),
            )
            .order_by(table.created_at.asc(), table.id.asc())
        ).tuples()
        return [
            ComputeReserveInstance(pool_id=pool_id, instance_id=instance_id, machine_id=machine_id)
            for pool_id, instance_id, machine_id in rows
            if pool_id is not None and instance_id is not None and machine_id is not None
        ]

    def platform_reserve_in_preparation(self) -> bool:
        table = ComputeProviderInstanceTable
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        table.pool_id == ComputeUnitTable.id,
                        table.status.in_(("preparing", "stopping")),
                        table.missing_since.is_(None),
                        ComputeUnitTable.platform_fleet.is_(True),
                    )
                )
            )
        )

    def record_prepared_release(
        self, *, machine_id: str, instance_id: str, agent_sha256: str, worker_image: str
    ) -> None:
        table = ComputeProviderInstanceTable
        self.session.execute(
            update(table)
            .where(table.machine_id == machine_id, table.instance_id == instance_id)
            .values(
                prepared_agent_sha256=agent_sha256,
                prepared_worker_image=worker_image,
                updated_at=utc_now(),
            )
        )

    def get_by_machine(self, machine_id: str) -> ComputeProviderInstanceRecord | None:
        row = self.session.scalars(
            select(ComputeProviderInstanceTable).where(
                ComputeProviderInstanceTable.machine_id == machine_id
            )
        ).one_or_none()
        return _provider_instance_record(row) if row is not None else None


def _join_credential_record(row: ComputeJoinCredentialTable) -> ComputeJoinCredentialRecord:
    return ComputeJoinCredentialRecord.model_validate(
        {
            "id": row.id,
            "token_hash": row.token_hash,
            "user_id": row.user_id,
            "workspace_id": row.workspace_id,
            "capacity_owner_id": row.capacity_owner_id,
            "placement": Placement.parse(row.placement),
            "machine_id": row.machine_id,
            "created_by_token_id": row.created_by_token_id,
            "status": row.status,
            "max_uses": row.max_uses,
            "use_count": row.use_count,
            "expires_at": to_utc(row.expires_at),
            "revoked_at": to_utc_or_none(row.revoked_at),
            "created_at": to_utc(row.created_at),
            "updated_at": to_utc(row.updated_at),
        }
    )


def _write_join_credential_row(
    row: ComputeJoinCredentialTable, record: ComputeJoinCredentialRecord
) -> None:
    row.token_hash = record.token_hash
    row.user_id = record.user_id
    row.workspace_id = record.workspace_id
    row.capacity_owner_id = record.capacity_owner_id
    row.placement = record.placement.key
    row.machine_id = record.machine_id
    row.created_by_token_id = record.created_by_token_id
    row.status = record.status.value
    row.max_uses = record.max_uses
    row.use_count = record.use_count
    row.expires_at = record.expires_at
    row.revoked_at = record.revoked_at
    row.created_at = record.created_at
    row.updated_at = record.updated_at


@dataclass(slots=True)
class ComputeJoinCredentialRepository:
    session: Session

    def create(
        self,
        *,
        token_hash: str,
        user_id: str | None,
        workspace_id: str,
        capacity_owner_id: str,
        placement: Placement,
        machine_id: str = "",
        created_by_token_id: str | None,
        max_uses: int,
        expires_at: datetime,
    ) -> ComputeJoinCredentialRecord:
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        now = utc_now()
        record = ComputeJoinCredentialRecord(
            id=str(uuid4()),
            token_hash=token_hash,
            user_id=user_id,
            workspace_id=workspace_id,
            capacity_owner_id=capacity_owner_id,
            placement=placement,
            machine_id=machine_id,
            created_by_token_id=created_by_token_id,
            max_uses=max(max_uses, 1),
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        row = ComputeJoinCredentialTable(id=record.id)
        _write_join_credential_row(row, record)
        self.session.add(row)
        self.session.flush()
        return _join_credential_record(row)

    def lock_unit(self, workspace_id: str, capacity_owner_id: str) -> bool:
        """Fence the unit a credential is minted against for the mint's duration."""
        statement = (
            select(ComputeUnitTable.id)
            .where(
                ComputeUnitTable.workspace_id == workspace_id,
                ComputeUnitTable.capacity_owner_id == capacity_owner_id,
            )
            .with_for_update()
        )
        return self.session.scalar(statement) is not None

    def get_by_hash(
        self,
        token_hash: str,
        *,
        for_update: bool = False,
    ) -> ComputeJoinCredentialRecord | None:
        statement = select(ComputeJoinCredentialTable).where(
            ComputeJoinCredentialTable.token_hash == token_hash
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return _join_credential_record(row) if row is not None else None

    def get(
        self,
        credential_id: str,
        *,
        for_update: bool = False,
    ) -> ComputeJoinCredentialRecord | None:
        statement = select(ComputeJoinCredentialTable).where(
            ComputeJoinCredentialTable.id == credential_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return _join_credential_record(row) if row is not None else None

    def list_for_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        *,
        for_update: bool = False,
    ) -> list[ComputeJoinCredentialRecord]:
        """Credentials one unit issued.

        Keyed by unit rather than by group: revoking a group's credentials would
        revoke every sibling unit's tokens along with them.
        """
        statement = select(ComputeJoinCredentialTable).where(
            ComputeJoinCredentialTable.workspace_id == workspace_id,
            ComputeJoinCredentialTable.capacity_owner_id == capacity_owner_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return [_join_credential_record(row) for row in self.session.scalars(statement)]

    def save(self, record: ComputeJoinCredentialRecord) -> ComputeJoinCredentialRecord:
        record = ComputeJoinCredentialRecord.model_validate(dict(record))
        WorkspaceRepository(self.session).lock_active_owner(record.workspace_id)
        row = self.session.get(ComputeJoinCredentialTable, record.id)
        if row is None:
            raise LookupError(f"compute join credential does not exist: {record.id}")
        if (
            row.workspace_id != record.workspace_id
            or row.user_id != record.user_id
            or row.token_hash != record.token_hash
            or row.capacity_owner_id != record.capacity_owner_id
            or row.machine_id != record.machine_id
        ):
            raise ConflictError("compute join credential authority cannot change")
        _write_join_credential_row(row, record)
        self.session.flush()
        return _join_credential_record(row)

    def save_for_workspace_deletion(
        self,
        record: ComputeJoinCredentialRecord,
    ) -> ComputeJoinCredentialRecord:
        workspace = WorkspaceRepository(self.session).lock_for_deletion(record.workspace_id)
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(f"workspace cleanup requires deleting state: {record.workspace_id}")
        row = self.session.scalars(
            select(ComputeJoinCredentialTable)
            .where(
                ComputeJoinCredentialTable.token_hash == record.token_hash,
                ComputeJoinCredentialTable.workspace_id == record.workspace_id,
            )
            .with_for_update()
        ).first()
        if row is None:
            raise LookupError(f"compute join credential does not exist: {record.id}")
        record = ComputeJoinCredentialRecord.model_validate(dict(record))
        row.status = record.status.value
        row.use_count = record.use_count
        row.expires_at = record.expires_at
        row.revoked_at = record.revoked_at
        row.updated_at = record.updated_at
        self.session.flush()
        return _join_credential_record(row)

    def delete_for_unit(self, workspace_id: str, capacity_owner_id: str) -> int:
        ids = list(
            self.session.scalars(
                select(ComputeJoinCredentialTable.id).where(
                    ComputeJoinCredentialTable.workspace_id == workspace_id,
                    ComputeJoinCredentialTable.capacity_owner_id == capacity_owner_id,
                )
            )
        )
        self.session.execute(
            delete(ComputeJoinCredentialTable).where(
                ComputeJoinCredentialTable.workspace_id == workspace_id,
                ComputeJoinCredentialTable.capacity_owner_id == capacity_owner_id,
            )
        )
        self.session.flush()
        return len(ids)


def _machine_enrollment_record(
    row: ComputeMachineEnrollmentTable,
) -> ComputeMachineEnrollmentRecord:
    return ComputeMachineEnrollmentRecord.model_validate(
        {
            "id": row.id,
            "user_id": row.user_id,
            "workspace_id": row.workspace_id,
            "capacity_owner_id": row.capacity_owner_id,
            "placement": Placement.parse(row.placement),
            "machine_id": row.machine_id,
            "machine_fingerprint_hash": row.machine_fingerprint_hash,
            "join_credential_id": row.join_credential_id,
            "credential_hash": row.credential_hash,
            "credential_generation": row.credential_generation,
            "tunnel_public_key_sha256": row.tunnel_public_key_sha256,
            "status": row.status,
            "preflight_passed": row.preflight_passed,
            "heartbeat_confirmed": row.heartbeat_confirmed,
            "schedulable": row.schedulable,
            "capacity_state": row.capacity_state,
            "capacity_reason": row.capacity_reason,
            "capacity_observed_at": to_utc_or_none(row.capacity_observed_at),
            "capacity_notice_at": to_utc_or_none(row.capacity_notice_at),
            "readiness_phase": row.readiness_phase,
            "hostname": row.hostname,
            "os": row.os,
            "arch": row.arch,
            "cpu_count": row.cpu_count,
            "cpu_millicores": row.cpu_millicores,
            "memory_mb": row.memory_mb,
            "gpus": row.gpus,
            "gpu_ids": row.gpu_ids,
            "gpu_count": row.gpu_count,
            "executor": row.executor,
            "preflight": row.preflight_checks,
            "agent_version": row.agent_version,
            "last_join_at": to_utc(row.last_join_at),
            "last_heartbeat_at": to_utc_or_none(row.last_heartbeat_at),
            "last_disconnect_at": to_utc_or_none(row.last_disconnect_at),
            "revoked_at": to_utc_or_none(row.revoked_at),
            "created_at": to_utc(row.created_at),
            "updated_at": to_utc(row.updated_at),
        }
    )


def _write_machine_enrollment_row(
    row: ComputeMachineEnrollmentTable, record: ComputeMachineEnrollmentRecord
) -> None:
    row.user_id = record.user_id
    row.workspace_id = record.workspace_id
    row.capacity_owner_id = record.capacity_owner_id
    row.placement = record.placement.key
    row.machine_id = record.machine_id
    row.machine_fingerprint_hash = record.machine_fingerprint_hash
    row.join_credential_id = record.join_credential_id
    row.credential_hash = record.credential_hash
    row.credential_generation = record.credential_generation
    row.tunnel_public_key_sha256 = record.tunnel_public_key_sha256
    row.status = record.status.value
    row.preflight_passed = record.preflight_passed
    row.heartbeat_confirmed = record.heartbeat_confirmed
    row.schedulable = record.schedulable
    row.capacity_state = record.capacity_state.value
    row.capacity_reason = record.capacity_reason
    row.capacity_observed_at = record.capacity_observed_at
    row.capacity_notice_at = record.capacity_notice_at
    row.readiness_phase = record.readiness_phase.value
    row.hostname = record.hostname
    row.os = record.os
    row.arch = record.arch
    row.cpu_count = record.cpu_count
    row.cpu_millicores = record.cpu_millicores
    row.memory_mb = record.memory_mb
    row.gpus = list(record.gpus)
    row.gpu_ids = list(record.gpu_ids)
    row.gpu_count = record.gpu_count
    row.executor = record.executor
    row.preflight_checks = [check.model_dump(mode="json") for check in record.preflight]
    row.agent_version = record.agent_version
    row.last_join_at = record.last_join_at
    row.last_heartbeat_at = record.last_heartbeat_at
    row.last_disconnect_at = record.last_disconnect_at
    row.revoked_at = record.revoked_at
    row.created_at = record.created_at
    row.updated_at = record.updated_at


@dataclass(slots=True)
class ComputeMachineEnrollmentRepository:
    session: Session

    def delete(self, enrollment_id: str, *, workspace_id: str) -> None:
        self.session.execute(
            delete(ComputeMachineEnrollmentTable).where(
                ComputeMachineEnrollmentTable.id == enrollment_id,
                ComputeMachineEnrollmentTable.workspace_id == workspace_id,
            )
        )
        self.session.flush()

    def create(self, enrollment: ComputeMachineEnrollmentCreate) -> ComputeMachineEnrollmentRecord:
        WorkspaceRepository(self.session).lock_active_owner(enrollment.workspace_id)
        now = utc_now()
        record = ComputeMachineEnrollmentRecord.model_validate(
            {
                **enrollment.model_dump(),
                "id": str(uuid4()),
                "created_at": now,
                "updated_at": now,
            }
        )
        row = ComputeMachineEnrollmentTable(id=record.id)
        _write_machine_enrollment_row(row, record)
        self.session.add(row)
        self.session.flush()
        return _machine_enrollment_record(row)

    def by_id(
        self,
        enrollment_id: str,
        *,
        workspace_id: str,
        for_update: bool = False,
    ) -> ComputeMachineEnrollmentRecord | None:
        return self._one(
            select(ComputeMachineEnrollmentTable).where(
                ComputeMachineEnrollmentTable.id == enrollment_id,
                ComputeMachineEnrollmentTable.workspace_id == workspace_id,
            ),
            for_update=for_update,
        )

    def save(self, record: ComputeMachineEnrollmentRecord) -> ComputeMachineEnrollmentRecord:
        record = ComputeMachineEnrollmentRecord.model_validate(dict(record))
        WorkspaceRepository(self.session).lock_active_owner(record.workspace_id)
        row = self.session.get(ComputeMachineEnrollmentTable, record.id)
        if row is None:
            raise LookupError(f"compute machine enrollment does not exist: {record.id}")
        if row.workspace_id != record.workspace_id or row.user_id != record.user_id:
            raise ConflictError("compute machine enrollment owner cannot change")
        _write_machine_enrollment_row(row, record)
        self.session.flush()
        return _machine_enrollment_record(row)

    def save_for_workspace_deletion(
        self,
        record: ComputeMachineEnrollmentRecord,
    ) -> ComputeMachineEnrollmentRecord:
        workspace = WorkspaceRepository(self.session).lock_for_deletion(record.workspace_id)
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(f"workspace cleanup requires deleting state: {record.workspace_id}")
        row = self.session.scalars(
            select(ComputeMachineEnrollmentTable)
            .where(
                ComputeMachineEnrollmentTable.id == record.id,
                ComputeMachineEnrollmentTable.workspace_id == record.workspace_id,
            )
            .with_for_update()
        ).first()
        if row is None:
            raise LookupError(f"compute machine enrollment does not exist: {record.id}")
        record = ComputeMachineEnrollmentRecord.model_validate(dict(record))
        row.status = record.status.value
        row.credential_generation = record.credential_generation
        row.preflight_passed = record.preflight_passed
        row.heartbeat_confirmed = record.heartbeat_confirmed
        row.schedulable = record.schedulable
        row.capacity_state = record.capacity_state.value
        row.capacity_reason = record.capacity_reason
        row.capacity_observed_at = record.capacity_observed_at
        row.capacity_notice_at = record.capacity_notice_at
        row.readiness_phase = record.readiness_phase.value
        row.last_join_at = record.last_join_at
        row.last_heartbeat_at = record.last_heartbeat_at
        row.last_disconnect_at = record.last_disconnect_at
        row.revoked_at = record.revoked_at
        row.updated_at = record.updated_at
        self.session.flush()
        return _machine_enrollment_record(row)

    def list_active_capacity_interruptions(
        self,
    ) -> list[ComputeMachineCapacityInterruptionRecord]:
        rows = self.session.execute(
            select(
                ComputeMachineEnrollmentTable.id,
                ComputeMachineEnrollmentTable.credential_generation,
                ComputeMachineEnrollmentTable.workspace_id,
                ComputeMachineEnrollmentTable.placement,
                ComputeMachineEnrollmentTable.machine_id,
                ComputeMachineEnrollmentTable.capacity_state,
                ComputeMachineEnrollmentTable.capacity_reason,
                ComputeMachineEnrollmentTable.capacity_observed_at,
                ComputeMachineEnrollmentTable.capacity_notice_at,
            )
            .where(
                ComputeMachineEnrollmentTable.status == ComputeMachineEnrollmentStatus.Active.value,
                ComputeMachineEnrollmentTable.capacity_state.in_(
                    (
                        AgentCapacityState.Draining.value,
                        AgentCapacityState.Preempting.value,
                        AgentCapacityState.Cordoned.value,
                    )
                ),
                ComputeMachineEnrollmentTable.capacity_observed_at.is_not(None),
            )
            .order_by(
                ComputeMachineEnrollmentTable.created_at.desc(),
                ComputeMachineEnrollmentTable.id.asc(),
            )
        ).tuples()
        return [
            ComputeMachineCapacityInterruptionRecord(
                enrollment_id=enrollment_id,
                credential_generation=credential_generation,
                workspace_id=workspace_id,
                placement=Placement.parse(placement),
                machine_id=machine_id,
                state=AgentCapacityState(state),
                reason=reason,
                observed_at=to_utc(observed_at),
                notice_at=to_utc_or_none(notice_at),
            )
            for (
                enrollment_id,
                credential_generation,
                workspace_id,
                placement,
                machine_id,
                state,
                reason,
                observed_at,
                notice_at,
            ) in rows
            if observed_at is not None
        ]

    def credential_by_hash(self, credential_hash: str) -> ComputeMachineCredentialRecord | None:
        row = self.session.execute(
            select(
                ComputeMachineEnrollmentTable.id,
                ComputeMachineEnrollmentTable.user_id,
                ComputeMachineEnrollmentTable.credential_generation,
                ComputeMachineEnrollmentTable.status,
                ComputeMachineEnrollmentTable.schedulable,
                ComputeMachineEnrollmentTable.capacity_state,
                ComputeMachineEnrollmentTable.capacity_reason,
                ComputeMachineEnrollmentTable.capacity_observed_at.label("capacity_observed_at"),
                ComputeMachineEnrollmentTable.capacity_notice_at.label("capacity_notice_at"),
            ).where(ComputeMachineEnrollmentTable.credential_hash == credential_hash)
        ).one_or_none()
        return (
            ComputeMachineCredentialRecord.model_validate(row._mapping) if row is not None else None
        )

    def by_credential_hash(
        self,
        credential_hash: str,
        *,
        for_update: bool = False,
    ) -> ComputeMachineEnrollmentRecord | None:
        return self._one(
            select(ComputeMachineEnrollmentTable).where(
                ComputeMachineEnrollmentTable.credential_hash == credential_hash
            ),
            for_update=for_update,
        )

    def by_fingerprint(
        self,
        user_id: str | None,
        machine_fingerprint_hash: str,
        *,
        for_update: bool = False,
    ) -> ComputeMachineEnrollmentRecord | None:
        """The one enrollment a physical host holds in this account.

        Neither the group nor the workspace is part of the key: a host that
        re-joins naming either differently is the same machine, and admitting it
        twice would advertise the same CPUs as two workers.
        """
        return self._one(
            select(ComputeMachineEnrollmentTable).where(
                ComputeMachineEnrollmentTable.user_id == user_id,
                ComputeMachineEnrollmentTable.machine_fingerprint_hash == machine_fingerprint_hash,
            ),
            for_update=for_update,
        )

    def move_placement_for_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        placement: Placement,
    ) -> int:
        """Restamp every enrollment of one unit with the placement the unit moved to."""
        result = self.session.execute(
            update(ComputeMachineEnrollmentTable)
            .where(
                ComputeMachineEnrollmentTable.workspace_id == workspace_id,
                ComputeMachineEnrollmentTable.capacity_owner_id == capacity_owner_id,
                ComputeMachineEnrollmentTable.placement != placement.key,
            )
            .values(placement=placement.key, updated_at=utc_now())
        )
        return int(result.rowcount) if isinstance(result, CursorResult) else 0

    def list_by_machine_ids(
        self, machine_ids: Collection[str]
    ) -> dict[str, ComputeMachineEnrollmentRecord]:
        """The enrollment of each of these machines, keyed by machine id."""
        if not machine_ids:
            return {}
        rows = self.session.scalars(
            select(ComputeMachineEnrollmentTable).where(
                ComputeMachineEnrollmentTable.machine_id.in_(list(machine_ids))
            )
        )
        return {row.machine_id: _machine_enrollment_record(row) for row in rows}

    def list_for_user(self, user_id: str) -> list[ComputeMachineEnrollmentRecord]:
        """Every machine this account owns, across the workspaces it holds."""
        statement = (
            select(ComputeMachineEnrollmentTable)
            .where(ComputeMachineEnrollmentTable.user_id == user_id)
            .order_by(ComputeMachineEnrollmentTable.created_at.asc())
        )
        return [_machine_enrollment_record(row) for row in self.session.scalars(statement)]

    def list_silent_since(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> list[ComputeMachineEnrollmentRecord]:
        """Active machines last seen before `cutoff` and not already marked gone.

        Control-plane-wide rather than workspace-scoped: a machine going quiet is
        found by sweeping every enrollment, not by a tenant asking.

        Last-seen is the later of the join and the heartbeat, spelled out column
        by column because the multi-argument `greatest` is PostgreSQL-only and
        this schema is also built on SQLite. `last_join_at` is not nullable, so
        the pair reduces to: the join is old, and any heartbeat is older still.

        Rows already carrying a disconnect at or after their last-seen are left
        out. They are the ones a sweep would decide nothing about, and excluding
        them here keeps each pass proportional to the machines actually leaving
        rather than to the fleet.
        """

        heartbeat = ComputeMachineEnrollmentTable.last_heartbeat_at
        joined = ComputeMachineEnrollmentTable.last_join_at
        disconnected = ComputeMachineEnrollmentTable.last_disconnect_at
        statement = (
            select(ComputeMachineEnrollmentTable)
            .where(
                ComputeMachineEnrollmentTable.status == ComputeMachineEnrollmentStatus.Active.value,
                or_(heartbeat.is_(None), heartbeat < cutoff),
                joined < cutoff,
                or_(
                    disconnected.is_(None),
                    disconnected < joined,
                    and_(heartbeat.is_not(None), disconnected < heartbeat),
                ),
            )
            .order_by(joined.asc(), ComputeMachineEnrollmentTable.id.asc())
            .limit(max(limit, 1))
        )
        return [_machine_enrollment_record(row) for row in self.session.scalars(statement)]

    def by_machine(
        self,
        workspace_id: str,
        machine_id: str,
        *,
        placement: Placement | None = None,
        for_update: bool = False,
    ) -> ComputeMachineEnrollmentRecord | None:
        statement = select(ComputeMachineEnrollmentTable).where(
            ComputeMachineEnrollmentTable.workspace_id == workspace_id,
            ComputeMachineEnrollmentTable.machine_id == machine_id,
        )
        if placement is not None:
            statement = statement.where(ComputeMachineEnrollmentTable.placement == placement.key)
        return self._one(statement, for_update=for_update)

    def list_for_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> list[ComputeMachineEnrollmentRecord]:
        statement = (
            select(ComputeMachineEnrollmentTable)
            .where(
                ComputeMachineEnrollmentTable.workspace_id == workspace_id,
                ComputeMachineEnrollmentTable.capacity_owner_id == capacity_owner_id,
            )
            .order_by(ComputeMachineEnrollmentTable.created_at.asc())
        )
        return [_machine_enrollment_record(row) for row in self.session.scalars(statement)]

    def delete_for_unit(self, workspace_id: str, capacity_owner_id: str) -> int:
        ids = list(
            self.session.scalars(
                select(ComputeMachineEnrollmentTable.id).where(
                    ComputeMachineEnrollmentTable.workspace_id == workspace_id,
                    ComputeMachineEnrollmentTable.capacity_owner_id == capacity_owner_id,
                )
            )
        )
        self.session.execute(
            delete(ComputeMachineEnrollmentTable).where(
                ComputeMachineEnrollmentTable.workspace_id == workspace_id,
                ComputeMachineEnrollmentTable.capacity_owner_id == capacity_owner_id,
            )
        )
        self.session.flush()
        return len(ids)

    def _one(
        self,
        statement: Select[tuple[ComputeMachineEnrollmentTable]],
        *,
        for_update: bool,
    ) -> ComputeMachineEnrollmentRecord | None:
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return _machine_enrollment_record(row) if row is not None else None


def _same_shape(unit: type[ComputeUnitTable]) -> ColumnElement[bool]:
    shape = ComputeNodeShapeTable
    return and_(
        shape.cpu_millicores == unit.worker_cpu_millicores,
        shape.memory_mib == unit.worker_memory_mib,
        shape.gpu_count == unit.worker_gpu_count,
    )


def _reported_memory() -> ColumnElement[int]:
    return func.coalesce(ComputeNodeShapeTable.reported_memory_mib, 0)
