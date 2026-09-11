from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from database.repositories.common import (
    GlobalTableRepository,
    TableRepositoryConfig,
    WorkspaceTableRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.tables.base import IdPayloadTable
from database.tables.billing_ledger import ContainerBillingShapeTable
from database.tables.compute import (
    AwsAccountConnectionTable,
    AwsAuthorizationCleanupTombstoneTable,
    ComputeCapacityOperationTable,
    ComputeJoinCredentialTable,
    ComputeMachineEnrollmentTable,
    ComputeProviderInstanceTable,
    ComputeUnitTable,
    WireGuardGatewayTable,
    WireGuardPeerTable,
    WorkspaceComputePolicyTable,
)
from database.tables.identity import WorkspaceMemberTable, WorkspaceTable
from pydantic import BaseModel, Field, JsonValue, TypeAdapter
from shared.aws_connections import (
    AwsAccountConnection,
    AwsAuthorizationCleanupTombstone,
)
from shared.capacity import TERMINAL_REASON_MAX_LENGTH, CapacityFailureCode, CapacityOwnerKind
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputeCredentialStatus,
    ComputeMachineEnrollmentStatus,
    ComputePreflightCheck,
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
    MachineReadinessPhase,
    PrivateNetworkEnrollmentPhase,
    WireGuardGateway,
    WireGuardPeer,
    WireGuardPeerStatus,
)
from shared.compute_policy import (
    ComputeUnitPhase,
    ComputeUnitProviderState,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    MachinePool,
    WorkspaceComputePolicy,
)
from shared.compute_reconciliation import ComputeReconciliationKind
from shared.contracts import ContractModel
from shared.errors import ConflictError
from shared.identity import WorkspaceRole, WorkspaceStatus
from shared.placement import placement_rate_class
from shared.supplier_costs import SupplierCostTerms, SupplierCpuUnit
from shared.timestamps import to_utc, utc_now
from sqlalchemy import (
    Select,
    String,
    and_,
    case,
    cast,
    delete,
    exists,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, load_only
from sqlalchemy.orm.attributes import flag_modified

type DatabaseInsertValue = JsonValue | datetime

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])
_DATETIME_ADAPTER = TypeAdapter(datetime)


def _model_json(model: BaseModel) -> dict[str, JsonValue]:
    return _JSON_OBJECT_ADAPTER.validate_json(model.model_dump_json())


class ComputeCapacityOperationRecord(ContractModel):
    id: str
    workspace_id: str
    pool_id: str
    capacity_owner_id: str
    reservation_id: str
    operation_id: str
    desired_unit: int = Field(ge=1)
    status: str
    target_machine_id: str | None = None
    provider_instance_id: str | None = None
    previous_desired_unit: int = Field(default=0, ge=0)
    release_desired_unit: int | None = Field(default=None, ge=0)
    owns_capacity: bool = False
    join_attempt: int = Field(default=1, ge=1)
    shape: dict[str, JsonValue] = Field(default_factory=dict)
    failure_code: CapacityFailureCode | None = None
    failure_count: int = Field(default=0, ge=0)
    last_error: str = Field(default="", max_length=TERMINAL_REASON_MAX_LENGTH)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ComputeCapacityOperationSizingRecord:
    operation_id: str
    desired_unit: int
    status: str
    owns_capacity: bool
    failure_count: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PlatformCpuArrival:
    created_at: datetime
    cpu_millicores: int
    reserved_memory_mib: int


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
    bootstrap_phase: MachineBootstrapPhase = MachineBootstrapPhase.Requested
    bootstrap_failure_reason: MachineBootstrapFailureReason | None = None
    bootstrap_failure_detail: str = ""
    bootstrap_observed_at: datetime = Field(default_factory=utc_now)
    bootstrap_phase_started_at: datetime | None = None
    """Phase-entry time, unaffected by repeated node observations."""
    # What the platform concluded, beside what the node reported above. A node
    # cannot observe that it serves workloads, so these are stamped by the
    # reconcile that watches it rather than by anything the machine says. They
    # are what lets the reclaim tell a machine that never worked from one that
    # worked and stopped, which the bootstrap phase alone cannot express.
    first_enrolled_at: datetime | None = None
    first_served_at: datetime | None = None
    last_served_at: datetime | None = None
    unserved_observations: int = Field(default=0, ge=0)
    launch_attempt: int = Field(default=1, ge=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    user_id: str
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
    pool: MachinePool
    """Pool the joining machine lands in, not the issuing unit's name."""
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
    user_id: str
    credential_generation: int
    status: ComputeMachineEnrollmentStatus
    schedulable: bool
    capacity_state: AgentCapacityState
    capacity_reason: str
    capacity_observed_at: datetime | None
    capacity_notice_at: datetime | None


class ComputeMachineEnrollmentRecord(ContractModel):
    id: str
    user_id: str
    """Account this machine belongs to, stamped from the credential that enrolled it."""
    workspace_id: str
    capacity_owner_id: str
    pool: MachinePool
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
    network_generation: int = Field(default=0, ge=0)
    network_phase: PrivateNetworkEnrollmentPhase = PrivateNetworkEnrollmentPhase.Unconfigured
    network_peer_id: str = ""
    network_public_key: str = ""
    network_address: str = ""
    network_verified_at: datetime | None = None
    network_failure_detail: str = Field(default="", max_length=512)
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
    pool: MachinePool
    machine_id: str
    state: AgentCapacityState
    reason: str
    observed_at: datetime
    notice_at: datetime | None


class ComputeMachineEnrollmentCreate(ContractModel):
    user_id: str
    workspace_id: str
    capacity_owner_id: str
    pool: MachinePool
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
    network_generation: int = Field(default=0, ge=0)
    network_phase: PrivateNetworkEnrollmentPhase = PrivateNetworkEnrollmentPhase.Unconfigured
    network_peer_id: str = ""
    network_public_key: str = ""
    network_address: str = ""
    network_verified_at: datetime | None = None
    network_failure_detail: str = Field(default="", max_length=512)
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
        """Re-state the row from a fresh join, keeping its WireGuard peer."""
        return existing.model_copy(
            update={
                **{
                    field: value
                    for field, value in dict(self).items()
                    if not field.startswith("network_")
                },
                "updated_at": updated_at,
            }
        )


class _ClaimedRecord(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def revision(self) -> int: ...

    @property
    def claim_token(self) -> str | None: ...


def _locked_claimed_row[RowT: IdPayloadTable, RecordT: _ClaimedRecord](
    session: Session,
    table: type[RowT],
    validate: Callable[[object], RecordT],
    claimed: RecordT,
) -> RowT | None:
    """Lock the row a claim names, or return None when the claim is no longer current."""

    row = session.scalars(select(table).where(table.id == claimed.id).with_for_update()).first()
    if row is None:
        return None
    current = validate(row.payload)
    if current.revision != claimed.revision or current.claim_token != claimed.claim_token:
        return None
    return row


def _compute_unit_record(row: ComputeUnitTable) -> ComputeUnitRecord:
    return ComputeUnitRecord.model_validate(row.payload)


@dataclass(slots=True)
class ComputeUnitRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ComputeUnitRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(ComputeUnitTable, ComputeUnitRecord),
        )

    def upsert(self, record: ComputeUnitRecord) -> ComputeUnitRecord:
        # The immutability comparison below is by identity, and it runs before the
        # store's own validation, so the record has to be typed by the time it
        # gets there or an unchanged owner reads as a changed one. `dict(record)`
        # rather than `model_dump`: dumping serializes, and a drifted record would
        # raise the serializer warning here instead of where it was introduced.
        record = ComputeUnitRecord.model_validate(dict(record))
        current = self.get(record.id, for_update=True)
        if current is not None:
            # The row owns its creation time; a caller rebuilding the record
            # from scratch must not be able to move it.
            record = record.model_copy(update={"created_at": current.created_at})
        if current is not None and (
            current.capacity_owner_id != record.capacity_owner_id
            or current.capacity_owner_kind is not record.capacity_owner_kind
            or current.capacity_owner_source is not record.capacity_owner_source
        ):
            raise ConflictError(f"compute pool capacity owner is immutable: {record.id}")
        saved = self.records.upsert(
            record,
            workspace_id=record.workspace_id,
            name=record.name,
            status=record.status,
        )
        self._write_columns(saved)
        return saved

    def get_by_name(
        self,
        workspace_id: str,
        name: str,
        *,
        for_update: bool = False,
    ) -> ComputeUnitRecord | None:
        """Resolve a unit by name, which only a create path may do.

        A name is this table's creation-time natural key and nothing else.
        Runtime callers address a unit by `id`/`capacity_owner_id`, because a
        pool label and a unit name are both free-form strings and keying on the
        name lets one be passed where the other was meant.
        """
        statement = select(ComputeUnitTable).where(
            ComputeUnitTable.workspace_id == workspace_id,
            ComputeUnitTable.name == name,
        )
        if for_update:
            statement = statement.with_for_update()
        statement = statement.options(load_only(ComputeUnitTable.payload, raiseload=True))
        row = self.session.scalars(statement).one_or_none()
        return _compute_unit_record(row) if row is not None else None

    def get_by_capacity_owner_id(
        self,
        capacity_owner_id: str,
        *,
        for_update: bool = False,
    ) -> ComputeUnitRecord | None:
        """Resolve one unit by its owner.

        Unscoped because the owner id is globally unique
        (`uq_compute_units_capacity_owner_id`) and the scheduler plane resolves
        units without a workspace in hand. Tenant-scoped callers check
        `workspace_id` on the row they get back.
        """
        statement = select(ComputeUnitTable).where(
            ComputeUnitTable.capacity_owner_id == capacity_owner_id
        )
        if for_update:
            statement = statement.with_for_update()
        statement = statement.options(load_only(ComputeUnitTable.payload, raiseload=True))
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

    def get(self, pool_id: str, *, for_update: bool = False) -> ComputeUnitRecord | None:
        """System lookup by pool id for placement/capacity reconciliation."""
        statement = select(ComputeUnitTable).where(ComputeUnitTable.id == pool_id)
        if for_update:
            statement = statement.with_for_update()
        statement = statement.options(load_only(ComputeUnitTable.payload, raiseload=True))
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
        statement = statement.options(load_only(ComputeUnitTable.payload, raiseload=True))
        row = self.session.scalars(statement).first()
        return _compute_unit_record(row) if row is not None else None

    def list_for_machine_pool(
        self,
        workspace_id: str,
        pool: MachinePool,
    ) -> list[ComputeUnitRecord]:
        """Every unit feeding one scheduling group, best candidate first."""
        statement = (
            select(ComputeUnitTable)
            .options(load_only(ComputeUnitTable.payload, raiseload=True))
            .where(
                ComputeUnitTable.workspace_id == workspace_id,
                ComputeUnitTable.pool == pool,
            )
            .order_by(ComputeUnitTable.priority.desc(), ComputeUnitTable.id)
        )
        return [_compute_unit_record(row) for row in self.session.scalars(statement)]

    def list_for_workspace(self, workspace_id: str) -> list[ComputeUnitRecord]:
        statement = (
            select(ComputeUnitTable)
            .options(load_only(ComputeUnitTable.payload, raiseload=True))
            .where(ComputeUnitTable.workspace_id == workspace_id)
            .order_by(ComputeUnitTable.created_at, ComputeUnitTable.id)
        )
        return [_compute_unit_record(row) for row in self.session.scalars(statement)]

    def list_across_workspaces(
        self, *, capacity_owner_kind: CapacityOwnerKind | None = None
    ) -> list[ComputeUnitRecord]:
        """System listing every unit, for scheduler controller construction."""
        statement = select(ComputeUnitTable).order_by(
            ComputeUnitTable.workspace_id,
            ComputeUnitTable.id,
        )
        if capacity_owner_kind is not None:
            statement = statement.where(
                ComputeUnitTable.capacity_owner_kind == capacity_owner_kind.value
            )
        statement = statement.options(load_only(ComputeUnitTable.payload, raiseload=True))
        return [_compute_unit_record(row) for row in self.session.scalars(statement)]

    def list_internal(self, *, workspace_id: str) -> list[ComputeUnitRecord]:
        return self._list_internal(workspace_id=workspace_id)

    def list_internal_across_workspaces(self) -> list[ComputeUnitRecord]:
        """System listing over every workspace's internal placement pools."""
        return self._list_internal(workspace_id=None)

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
            ComputeUnitTable.observed_machines > 0,
            ComputeUnitTable.phase == ComputeUnitPhase.Deleting.value,
            exists().where(
                ComputeProviderInstanceTable.pool_id == ComputeUnitTable.id,
                ComputeProviderInstanceTable.status.not_in(("deleted", "failed")),
            ),
        )
        base = (
            select(ComputeUnitTable)
            .where(ComputeUnitTable.visibility == ComputeUnitVisibility.Internal.value)
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
            ComputeUnitTable.payload["platform_fleet"].as_boolean().is_(True),
        )
        if preemptible is not None:
            statement = statement.where(ComputeUnitTable.worker_preemptible.is_(preemptible))
        if gpu is not None:
            statement = statement.where(
                ComputeUnitTable.worker_gpu_count > 0
                if gpu
                else ComputeUnitTable.worker_gpu_count == 0
            )
        statement = statement.order_by(ComputeUnitTable.updated_at, ComputeUnitTable.id).options(
            load_only(ComputeUnitTable.payload, raiseload=True)
        )
        return [_compute_unit_record(row) for row in self.session.scalars(statement)]

    def _list_internal(self, *, workspace_id: str | None) -> list[ComputeUnitRecord]:
        statement = select(ComputeUnitTable).where(
            ComputeUnitTable.visibility == ComputeUnitVisibility.Internal.value
        )
        if workspace_id is not None:
            statement = statement.where(ComputeUnitTable.workspace_id == workspace_id)
        statement = statement.order_by(ComputeUnitTable.updated_at, ComputeUnitTable.id)
        statement = statement.options(load_only(ComputeUnitTable.payload, raiseload=True))
        return [_compute_unit_record(row) for row in self.session.scalars(statement)]

    def list_for_provider_connection(self, connection_id: str) -> list[ComputeUnitRecord]:
        statement = (
            select(ComputeUnitTable)
            .options(load_only(ComputeUnitTable.payload, raiseload=True))
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
                        != func.coalesce(
                            ComputeUnitTable.payload["replacement_machine_id"].as_string(), ""
                        ),
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
                func.coalesce(ComputeUnitTable.payload["replacement_machine_id"].as_string(), "")
                != "",
                1,
            ),
            else_=0,
        )
        statement = (
            select(
                func.coalesce(
                    func.sum(
                        func.greatest(
                            ComputeUnitTable.desired_machines
                            + surge
                            + func.coalesce(live_instances.c.retiring_count, 0),
                            ComputeUnitTable.observed_machines,
                            func.coalesce(live_instances.c.count, 0),
                        )
                    ),
                    0,
                )
            )
            .outerjoin(live_instances, live_instances.c.pool_id == ComputeUnitTable.id)
            .where(
                ComputeUnitTable.visibility == ComputeUnitVisibility.Internal.value,
                ComputeUnitTable.payload["platform_fleet"].as_boolean().is_(True),
                (
                    ComputeUnitTable.worker_gpu_count > 0
                    if gpu
                    else ComputeUnitTable.worker_gpu_count == 0
                ),
            )
        )
        if excluding_unit_id is not None:
            statement = statement.where(ComputeUnitTable.id != excluding_unit_id)
        return int(self.session.scalar(statement) or 0)

    def recent_platform_cpu_arrivals(
        self, since: datetime, *, preemptible: bool
    ) -> list[PlatformCpuArrival]:
        statement = (
            select(ContainerBillingShapeTable)
            .where(
                ContainerBillingShapeTable.billing_owner == "platform_fleet",
                ContainerBillingShapeTable.rate_class
                == placement_rate_class(pinned=False, preemptible=preemptible),
                ContainerBillingShapeTable.gpu_count == 0,
                ContainerBillingShapeTable.created_at >= since,
            )
            .order_by(ContainerBillingShapeTable.created_at)
        )
        return [
            PlatformCpuArrival(
                created_at=row.created_at,
                cpu_millicores=row.cpu_millicores,
                reserved_memory_mib=row.memory_mib,
            )
            for row in self.session.scalars(statement)
        ]

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
    ) -> ComputeUnitRecord | None:
        current = self.get(pool_id, for_update=True)
        if current is None or current.generation != expected_generation:
            return None
        updated = current.model_copy(
            update={
                "desired_machines": desired_machines,
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
        if current is None or current.generation != generation:
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

    def _write_columns(self, record: ComputeUnitRecord) -> None:
        row = self.session.scalars(
            select(ComputeUnitTable).where(ComputeUnitTable.id == record.id).with_for_update()
        ).one()
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
        row.pool = record.pool
        row.provider = record.provider
        row.desired_machines = record.desired_machines
        row.initial_machines = record.initial_machines
        row.min_machines = record.min_machines
        row.max_machines = record.max_machines
        row.observed_machines = record.observed_machines
        row.generation = record.generation
        row.phase = record.phase.value
        row.provider_state = _model_json(record.provider_state)
        flag_modified(row, "provider_state")
        row.scaling_enabled = record.scaling_enabled
        row.default_eligible = record.default_eligible
        row.priority = record.priority
        row.min_free_cpu_millicores = record.min_free_cpu_millicores
        row.min_free_memory_mib = record.min_free_memory_mib
        row.min_free_gpu_count = record.min_free_gpu_count
        row.worker_cpu_millicores = record.worker_cpu_millicores
        row.worker_memory_mib = record.worker_memory_mib
        row.worker_gpu_type = record.worker_gpu_type
        row.worker_gpu_count = record.worker_gpu_count
        row.worker_runtimes = list(record.worker_runtimes)
        flag_modified(row, "worker_runtimes")
        row.worker_preemptible = record.worker_preemptible
        row.idle_drain_timeout_seconds = record.idle_drain_timeout_seconds
        row.scale_up_cooldown_seconds = record.scale_up_cooldown_seconds
        row.scale_down_cooldown_seconds = record.scale_down_cooldown_seconds
        row.registration_timeout_seconds = record.registration_timeout_seconds
        row.root_volume_gib = record.root_volume_gib
        row.transport = record.transport.value
        row.fallback = record.fallback.value
        self.session.flush()


@dataclass(slots=True)
class WorkspaceComputePolicyRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[WorkspaceComputePolicy]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(WorkspaceComputePolicyTable, WorkspaceComputePolicy),
        )

    def create(self, policy: WorkspaceComputePolicy) -> WorkspaceComputePolicy:
        return self.records.create(
            policy.model_dump(mode="python"),
            workspace_id=policy.workspace_id,
        )

    def ensure_default(self, policy: WorkspaceComputePolicy) -> WorkspaceComputePolicy:
        values: dict[str, DatabaseInsertValue] = {
            "id": policy.id,
            "workspace_id": policy.workspace_id,
            "revision": policy.revision,
            "default_pool": policy.default_pool,
            "payload": _model_json(policy),
            "created_at": policy.created_at,
            "updated_at": policy.updated_at,
        }
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = (
                postgresql_insert(WorkspaceComputePolicyTable)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[WorkspaceComputePolicyTable.workspace_id])
            )
        elif dialect == "sqlite":
            statement = (
                sqlite_insert(WorkspaceComputePolicyTable)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[WorkspaceComputePolicyTable.workspace_id])
            )
        else:
            current = self.get_for_workspace(policy.workspace_id)
            return current or self.create(policy)
        self.session.execute(statement)
        self.session.flush()
        current = self.get_for_workspace(policy.workspace_id)
        if current is None:
            raise RuntimeError("workspace compute policy insert did not persist")
        return current

    def get_for_workspace(
        self,
        workspace_id: str,
        *,
        for_update: bool = False,
    ) -> WorkspaceComputePolicy | None:
        statement = select(WorkspaceComputePolicyTable).where(
            WorkspaceComputePolicyTable.workspace_id == workspace_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return WorkspaceComputePolicy.model_validate(row.payload) if row is not None else None

    def save(self, policy: WorkspaceComputePolicy) -> WorkspaceComputePolicy:
        saved = self.records.upsert(policy, workspace_id=policy.workspace_id)
        row = self.session.scalars(
            select(WorkspaceComputePolicyTable)
            .where(WorkspaceComputePolicyTable.id == policy.id)
            .with_for_update()
        ).one()
        row.revision = policy.revision
        row.default_pool = policy.default_pool
        self.session.flush()
        return saved


@dataclass(slots=True)
class ComputeCapacityOperationRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ComputeCapacityOperationRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(
                ComputeCapacityOperationTable,
                ComputeCapacityOperationRecord,
            ),
        )

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
        return (
            ComputeCapacityOperationRecord.model_validate(row.payload) if row is not None else None
        )

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
        return (
            ComputeCapacityOperationRecord.model_validate(row.payload) if row is not None else None
        )

    def list_open_for_owner(self, capacity_owner_id: str) -> list[ComputeCapacityOperationRecord]:
        rows = self.session.scalars(
            select(ComputeCapacityOperationTable)
            .where(
                ComputeCapacityOperationTable.capacity_owner_id == capacity_owner_id,
                ComputeCapacityOperationTable.status.not_in(("released", "unsupported")),
            )
            .order_by(ComputeCapacityOperationTable.created_at, ComputeCapacityOperationTable.id)
        )
        return [ComputeCapacityOperationRecord.model_validate(row.payload) for row in rows]

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
                    ComputeCapacityOperationTable.payload["owns_capacity"].as_boolean(),
                    False,
                ),
                func.coalesce(
                    ComputeCapacityOperationTable.payload["failure_count"].as_integer(),
                    0,
                ),
                ComputeCapacityOperationTable.payload["updated_at"].as_string(),
            )
            .where(
                ComputeCapacityOperationTable.capacity_owner_id == capacity_owner_id,
                ComputeCapacityOperationTable.status.not_in(("released", "unsupported")),
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
                status=status,
                owns_capacity=owns_capacity,
                failure_count=failure_count,
                updated_at=to_utc(_DATETIME_ADAPTER.validate_python(updated_at)),
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

    def list_for_owner(self, capacity_owner_id: str) -> list[ComputeCapacityOperationRecord]:
        rows = self.session.scalars(
            select(ComputeCapacityOperationTable)
            .where(ComputeCapacityOperationTable.capacity_owner_id == capacity_owner_id)
            .order_by(ComputeCapacityOperationTable.created_at, ComputeCapacityOperationTable.id)
        )
        return [ComputeCapacityOperationRecord.model_validate(row.payload) for row in rows]

    def sizing_history_summary_for_owner(
        self,
        capacity_owner_id: str,
    ) -> ComputeCapacityOperationHistorySummary:
        """Summarize all requests without loading their durable JSON payloads.

        Released operations stay in the aggregate so the peak remains monotonic.
        That stops the sizer from buying back every machine the drain controller
        retires.
        """
        peak_desired_unit, last_requested_at = self.session.execute(
            select(
                func.max(ComputeCapacityOperationTable.desired_unit),
                func.max(ComputeCapacityOperationTable.payload["created_at"].as_string()),
            ).where(ComputeCapacityOperationTable.capacity_owner_id == capacity_owner_id)
        ).one()
        return ComputeCapacityOperationHistorySummary(
            peak_desired_unit=int(peak_desired_unit or 0),
            last_requested_at=(
                to_utc(_DATETIME_ADAPTER.validate_python(last_requested_at))
                if last_requested_at is not None
                else None
            ),
        )

    def upsert(self, record: ComputeCapacityOperationRecord) -> ComputeCapacityOperationRecord:
        current = self.get(record.capacity_owner_id, record.operation_id, for_update=True)
        if current is not None and (
            current.reservation_id != record.reservation_id
            or current.pool_id != record.pool_id
            or current.workspace_id != record.workspace_id
            or current.desired_unit != record.desired_unit
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
        saved = self.records.upsert(
            record,
            workspace_id=record.workspace_id,
            status=record.status,
        )
        row = self.session.scalars(
            select(ComputeCapacityOperationTable)
            .where(ComputeCapacityOperationTable.id == record.id)
            .with_for_update()
        ).one()
        row.pool_id = saved.pool_id
        row.capacity_owner_id = saved.capacity_owner_id
        row.reservation_id = saved.reservation_id
        row.operation_id = saved.operation_id
        row.desired_unit = saved.desired_unit
        row.status = saved.status
        row.target_machine_id = saved.target_machine_id
        self.session.flush()
        return saved


def _provider_instance_record(
    row: ComputeProviderInstanceTable,
) -> ComputeProviderInstanceRecord:
    """Read one provider instance, taking its machine from the enforced column.

    The payload is the record and the column is the constraint, and only the
    column is maintained by the database: deleting a machine nulls it through
    `ON DELETE SET NULL` and cannot reach into the JSON beside it. A reader that
    trusted the payload would hand back a machine that no longer exists, and the
    next write would offer it to the foreign key that had just removed it —
    which every pass then fails on identically, so the pool degrades on an error
    it can never get past.
    """
    record = ComputeProviderInstanceRecord.model_validate(row.payload)
    if record.machine_id == row.machine_id:
        return record
    return record.model_copy(update={"machine_id": row.machine_id})


@dataclass(slots=True)
class ComputeProviderInstanceRepository:
    session: Session

    @property
    def records(self) -> GlobalTableRepository[ComputeProviderInstanceRecord]:
        return GlobalTableRepository(
            self.session,
            TableRepositoryConfig(ComputeProviderInstanceTable, ComputeProviderInstanceRecord),
        )

    def upsert(self, record: ComputeProviderInstanceRecord) -> ComputeProviderInstanceRecord:
        return self.records.upsert(record, status=record.status)

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
            .options(
                load_only(
                    ComputeProviderInstanceTable.payload,
                    ComputeProviderInstanceTable.machine_id,
                    raiseload=True,
                )
            )
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
        metadata = table.payload["metadata"]
        statement = (
            select(table)
            .options(load_only(table.payload, table.machine_id, raiseload=True))
            .where(
                table.pool_id == pool_id,
                or_(
                    table.status.not_in(terminal_statuses),
                    table.instance_id.in_(observed_instance_ids),
                    metadata["missing_since"].as_string().is_(None),
                    metadata["provider_storage_destroyed_at"].as_string().is_(None),
                ),
            )
            .order_by(table.created_at.desc(), table.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        return [_provider_instance_record(row) for row in self.session.scalars(statement)]

    def highest_launch_attempt(self, pool_id: str, *, default: int = 0) -> int:
        highest = self.session.scalar(
            select(
                func.max(
                    func.coalesce(
                        ComputeProviderInstanceTable.payload["launch_attempt"].as_integer(), 1
                    )
                )
            ).where(ComputeProviderInstanceTable.pool_id == pool_id)
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
        bound = current.model_copy(update={"machine_id": machine_id, "updated_at": utc_now()})
        row.machine_id = machine_id
        row.payload = _model_json(bound)
        flag_modified(row, "payload")
        self.session.flush()
        return bound

    def unbind_machine(
        self,
        pool_id: str,
        provider_instance_id: str,
        machine_id: str,
    ) -> ComputeProviderInstanceRecord | None:
        """Release a machine binding, so a torn-down machine leaves no reference.

        Bound only if the row still names this machine: a later enrollment may
        have rebound the instance, and clearing that binding would strand a live
        machine instead of the dead one.
        """
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
        if current.machine_id != machine_id:
            return current
        released = current.model_copy(update={"machine_id": None, "updated_at": utc_now()})
        row.machine_id = None
        row.payload = _model_json(released)
        flag_modified(row, "payload")
        self.session.flush()
        return released

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

    def get_by_machine(self, machine_id: str) -> ComputeProviderInstanceRecord | None:
        row = self.session.scalars(
            select(ComputeProviderInstanceTable).where(
                ComputeProviderInstanceTable.machine_id == machine_id
            )
        ).one_or_none()
        return _provider_instance_record(row) if row is not None else None


@dataclass(slots=True)
class ComputeJoinCredentialRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ComputeJoinCredentialRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(
                ComputeJoinCredentialTable,
                ComputeJoinCredentialRecord,
                key_field="token_hash",
            ),
        )

    def create(
        self,
        *,
        token_hash: str,
        user_id: str,
        workspace_id: str,
        capacity_owner_id: str,
        pool: MachinePool,
        machine_id: str = "",
        created_by_token_id: str | None,
        max_uses: int,
        expires_at: datetime,
    ) -> ComputeJoinCredentialRecord:
        return self.records.create(
            {
                "token_hash": token_hash,
                "user_id": user_id,
                "workspace_id": workspace_id,
                "capacity_owner_id": capacity_owner_id,
                "pool": pool,
                "machine_id": machine_id,
                "created_by_token_id": created_by_token_id,
                "status": ComputeCredentialStatus.Active,
                "max_uses": max(max_uses, 1),
                "use_count": 0,
                "expires_at": expires_at,
            },
            workspace_id=workspace_id,
            status=ComputeCredentialStatus.Active.value,
        )

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
        return ComputeJoinCredentialRecord.model_validate(row.payload) if row is not None else None

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
        return ComputeJoinCredentialRecord.model_validate(row.payload) if row is not None else None

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
        return [
            ComputeJoinCredentialRecord.model_validate(row.payload)
            for row in self.session.scalars(statement)
        ]

    def save(self, record: ComputeJoinCredentialRecord) -> ComputeJoinCredentialRecord:
        return self.records.upsert(
            record,
            key=record.token_hash,
            workspace_id=record.workspace_id,
            status=record.status.value,
        )

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
        row.payload = _model_json(record)
        row.status = record.status.value
        row.use_count = record.use_count
        row.expires_at = record.expires_at
        row.revoked_at = record.revoked_at
        flag_modified(row, "payload")
        self.session.flush()
        return record

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


@dataclass(slots=True)
class ComputeMachineEnrollmentRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ComputeMachineEnrollmentRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(
                ComputeMachineEnrollmentTable,
                ComputeMachineEnrollmentRecord,
            ),
        )

    def create(
        self,
        enrollment: ComputeMachineEnrollmentCreate,
    ) -> ComputeMachineEnrollmentRecord:
        return self.records.create(
            enrollment.model_dump(mode="python"),
            workspace_id=enrollment.workspace_id,
            status=enrollment.status.value,
        )

    def save(self, record: ComputeMachineEnrollmentRecord) -> ComputeMachineEnrollmentRecord:
        return self.records.upsert(
            record,
            workspace_id=record.workspace_id,
            status=record.status.value,
        )

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
        row.payload = _model_json(record)
        row.status = record.status.value
        row.credential_generation = record.credential_generation
        row.preflight_passed = record.preflight_passed
        row.heartbeat_confirmed = record.heartbeat_confirmed
        row.schedulable = record.schedulable
        row.capacity_state = record.capacity_state.value
        row.capacity_observed_at = record.capacity_observed_at
        row.readiness_phase = record.readiness_phase.value
        row.last_join_at = record.last_join_at
        row.last_heartbeat_at = record.last_heartbeat_at
        row.last_disconnect_at = record.last_disconnect_at
        row.revoked_at = record.revoked_at
        flag_modified(row, "payload")
        self.session.flush()
        return record

    def list_active_capacity_interruptions(
        self,
    ) -> list[ComputeMachineCapacityInterruptionRecord]:
        rows = self.session.execute(
            select(
                ComputeMachineEnrollmentTable.id,
                ComputeMachineEnrollmentTable.credential_generation,
                ComputeMachineEnrollmentTable.workspace_id,
                ComputeMachineEnrollmentTable.pool,
                ComputeMachineEnrollmentTable.machine_id,
                ComputeMachineEnrollmentTable.capacity_state,
                func.coalesce(
                    ComputeMachineEnrollmentTable.payload["capacity_reason"].as_string(),
                    "",
                ),
                ComputeMachineEnrollmentTable.capacity_observed_at,
                ComputeMachineEnrollmentTable.payload["capacity_notice_at"].as_string(),
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
                pool=MachinePool(pool),
                machine_id=machine_id,
                state=AgentCapacityState(state),
                reason=reason,
                observed_at=to_utc(observed_at),
                notice_at=_DATETIME_ADAPTER.validate_python(notice_at) if notice_at else None,
            )
            for (
                enrollment_id,
                credential_generation,
                workspace_id,
                pool,
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
                func.coalesce(
                    ComputeMachineEnrollmentTable.payload["capacity_reason"].as_string(), ""
                ).label("capacity_reason"),
                ComputeMachineEnrollmentTable.payload["capacity_observed_at"]
                .as_string()
                .label("capacity_observed_at"),
                ComputeMachineEnrollmentTable.payload["capacity_notice_at"]
                .as_string()
                .label("capacity_notice_at"),
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
        user_id: str,
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

    def list_for_user(self, user_id: str) -> list[ComputeMachineEnrollmentRecord]:
        """Every machine this account owns, across the workspaces it holds."""
        statement = (
            select(ComputeMachineEnrollmentTable)
            .where(ComputeMachineEnrollmentTable.user_id == user_id)
            .order_by(ComputeMachineEnrollmentTable.created_at.asc())
        )
        return [
            ComputeMachineEnrollmentRecord.model_validate(row.payload)
            for row in self.session.scalars(statement)
        ]

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
        return [
            ComputeMachineEnrollmentRecord.model_validate(row.payload)
            for row in self.session.scalars(statement)
        ]

    def by_machine(
        self,
        workspace_id: str,
        machine_id: str,
        *,
        pool: MachinePool = MachinePool(""),
        for_update: bool = False,
    ) -> ComputeMachineEnrollmentRecord | None:
        statement = select(ComputeMachineEnrollmentTable).where(
            ComputeMachineEnrollmentTable.workspace_id == workspace_id,
            ComputeMachineEnrollmentTable.machine_id == machine_id,
        )
        if pool:
            statement = statement.where(ComputeMachineEnrollmentTable.pool == pool)
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
        return [
            ComputeMachineEnrollmentRecord.model_validate(row.payload)
            for row in self.session.scalars(statement)
        ]

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
        statement = statement.options(
            load_only(ComputeMachineEnrollmentTable.payload, raiseload=True)
        )
        row = self.session.scalars(statement).first()
        return (
            ComputeMachineEnrollmentRecord.model_validate(row.payload) if row is not None else None
        )


@dataclass(slots=True)
class WireGuardPeerRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[WireGuardPeer]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(WireGuardPeerTable, WireGuardPeer),
        )

    def lock_allocator(self) -> None:
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            self.session.execute(select(func.pg_advisory_xact_lock(1_282_385_785)))

    def address_allocated(self, address: str) -> bool:
        return (
            self.session.scalar(
                select(WireGuardPeerTable.id).where(WireGuardPeerTable.address == address).limit(1)
            )
            is not None
        )

    def by_enrollment(
        self,
        enrollment_id: str,
        *,
        for_update: bool = False,
    ) -> WireGuardPeer | None:
        statement = select(WireGuardPeerTable).where(
            WireGuardPeerTable.enrollment_id == enrollment_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).one_or_none()
        return WireGuardPeer.model_validate(row.payload) if row is not None else None

    def active(self) -> list[WireGuardPeer]:
        rows = self.session.scalars(
            select(WireGuardPeerTable)
            .where(WireGuardPeerTable.status == WireGuardPeerStatus.Active.value)
            .order_by(WireGuardPeerTable.address)
        )
        return [WireGuardPeer.model_validate(row.payload) for row in rows]

    def save(self, peer: WireGuardPeer) -> WireGuardPeer:
        saved = self.records.upsert(
            peer,
            workspace_id=peer.workspace_id,
            status=peer.status.value,
        )
        row = self.session.scalars(
            select(WireGuardPeerTable).where(WireGuardPeerTable.id == peer.id)
        ).one()
        row.enrollment_id = saved.enrollment_id
        row.machine_id = saved.machine_id
        row.public_key = saved.public_key
        row.address = saved.address
        row.generation = saved.generation
        row.last_handshake_at = saved.last_handshake_at
        row.revoked_at = saved.revoked_at
        self.session.flush()
        return saved


PRIMARY_WIREGUARD_GATEWAY_ID = "3acde72c-e3ae-43d2-a119-cfeb8c0309be"


@dataclass(slots=True)
class WireGuardGatewayRepository:
    session: Session

    @property
    def records(self) -> GlobalTableRepository[WireGuardGateway]:
        return GlobalTableRepository(
            self.session,
            TableRepositoryConfig(WireGuardGatewayTable, WireGuardGateway),
        )

    def current(self) -> WireGuardGateway | None:
        return self.records.get(PRIMARY_WIREGUARD_GATEWAY_ID)

    def save(self, gateway: WireGuardGateway) -> WireGuardGateway:
        saved = self.records.upsert(gateway)
        row = self.session.scalars(
            select(WireGuardGatewayTable).where(WireGuardGatewayTable.id == gateway.id)
        ).one()
        row.public_key = saved.public_key
        row.endpoint = saved.endpoint
        self.session.flush()
        return saved


@dataclass(slots=True)
class AwsAccountConnectionRepository:
    session: Session

    def create(self, connection: AwsAccountConnection) -> AwsAccountConnection:
        row = AwsAccountConnectionTable(
            id=connection.id,
            user_id=connection.user_id,
            account_id=connection.account_id,
            external_id=connection.external_id,
            pool=connection.pool,
            phase=connection.phase.value,
            revision=connection.revision,
            next_reconcile_at=connection.next_reconcile_at,
            claim_token=connection.claim_token,
            claim_expires_at=connection.claim_expires_at,
            reconcile_attempt_count=connection.reconcile_attempt_count,
            provider_operation_id=connection.provider_operation_id,
            provider_operation_started_at=connection.provider_operation_started_at,
            payload=_model_json(connection),
            created_at=connection.created_at,
            updated_at=connection.updated_at,
        )
        self.session.add(row)
        self.session.flush()
        return connection

    def save(self, connection: AwsAccountConnection) -> AwsAccountConnection:
        row = self.session.get(AwsAccountConnectionTable, connection.id)
        if row is None:
            raise LookupError(f"AWS account connection {connection.id} does not exist")
        self._write(row, connection)
        return connection

    def get_for_user(
        self,
        user_id: str,
        *,
        for_update: bool = False,
    ) -> AwsAccountConnection | None:
        statement = select(AwsAccountConnectionTable).where(
            AwsAccountConnectionTable.user_id == user_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return AwsAccountConnection.model_validate(row.payload) if row is not None else None

    def get_for_workspace_owner(
        self,
        workspace_id: str,
        *,
        for_update: bool = False,
    ) -> AwsAccountConnection | None:
        """The connected account backing a workspace, reached through its owner.

        One join rather than two lookups so the owner cannot change between them, and
        so every caller asks the question the same way.
        """
        statement = (
            select(AwsAccountConnectionTable)
            .join(
                WorkspaceMemberTable,
                WorkspaceMemberTable.user_id == AwsAccountConnectionTable.user_id,
            )
            .where(
                WorkspaceMemberTable.workspace_id == workspace_id,
                WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
            )
        )
        if for_update:
            statement = statement.with_for_update(of=AwsAccountConnectionTable)
        row = self.session.scalars(statement).first()
        return AwsAccountConnection.model_validate(row.payload) if row is not None else None

    def get(
        self,
        connection_id: str,
        *,
        for_update: bool = False,
    ) -> AwsAccountConnection | None:
        statement = select(AwsAccountConnectionTable).where(
            AwsAccountConnectionTable.id == connection_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return AwsAccountConnection.model_validate(row.payload) if row is not None else None

    def list_for_user(self, user_id: str) -> list[AwsAccountConnection]:
        statement = (
            select(AwsAccountConnectionTable)
            .where(AwsAccountConnectionTable.user_id == user_id)
            .order_by(
                AwsAccountConnectionTable.created_at.desc(),
                AwsAccountConnectionTable.id.asc(),
            )
        )
        return [
            AwsAccountConnection.model_validate(row.payload)
            for row in self.session.scalars(statement)
        ]

    def list_all(self) -> list[AwsAccountConnection]:
        """Every connected account, for control-plane-wide reconciliation."""
        statement = select(AwsAccountConnectionTable).order_by(
            AwsAccountConnectionTable.created_at.asc(),
            AwsAccountConnectionTable.id.asc(),
        )
        return [
            AwsAccountConnection.model_validate(row.payload)
            for row in self.session.scalars(statement)
        ]

    def claim_due(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> list[AwsAccountConnection]:
        statement = (
            select(AwsAccountConnectionTable)
            .where(
                AwsAccountConnectionTable.next_reconcile_at.is_not(None),
                AwsAccountConnectionTable.next_reconcile_at <= now,
                or_(
                    AwsAccountConnectionTable.claim_expires_at.is_(None),
                    AwsAccountConnectionTable.claim_expires_at <= now,
                ),
            )
            .order_by(
                AwsAccountConnectionTable.next_reconcile_at.asc(),
                AwsAccountConnectionTable.id.asc(),
            )
            .limit(max(limit, 1))
            .with_for_update(skip_locked=True)
        )
        claimed: list[AwsAccountConnection] = []
        for row in self.session.scalars(statement):
            current = AwsAccountConnection.model_validate(row.payload)
            connection = current.model_copy(
                update={
                    "claim_token": str(uuid4()),
                    "claim_expires_at": lease_until,
                    "updated_at": now,
                }
            )
            self._write(row, connection)
            claimed.append(connection)
        return claimed

    def finish_claim(
        self,
        claimed: AwsAccountConnection,
        updated: AwsAccountConnection,
    ) -> AwsAccountConnection | None:
        row = self._claimed_row(claimed)
        if row is None:
            return None
        finished = updated.model_copy(
            update={
                "revision": claimed.revision + 1,
                "claim_token": None,
                "claim_expires_at": None,
            }
        )
        self._write(row, finished)
        return finished

    def delete_claimed(self, claimed: AwsAccountConnection) -> bool:
        row = self._claimed_row(claimed)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def delete(self, connection: AwsAccountConnection) -> None:
        self.session.execute(
            delete(AwsAccountConnectionTable).where(AwsAccountConnectionTable.id == connection.id)
        )
        self.session.flush()

    def _claimed_row(
        self,
        claimed: AwsAccountConnection,
    ) -> AwsAccountConnectionTable | None:
        return _locked_claimed_row(
            self.session,
            AwsAccountConnectionTable,
            AwsAccountConnection.model_validate,
            claimed,
        )

    def _write(
        self,
        row: AwsAccountConnectionTable,
        connection: AwsAccountConnection,
    ) -> None:
        row.user_id = connection.user_id
        row.account_id = connection.account_id
        row.external_id = connection.external_id
        row.pool = connection.pool
        row.phase = connection.phase.value
        row.revision = connection.revision
        row.next_reconcile_at = connection.next_reconcile_at
        row.claim_token = connection.claim_token
        row.claim_expires_at = connection.claim_expires_at
        row.reconcile_attempt_count = connection.reconcile_attempt_count
        row.provider_operation_id = connection.provider_operation_id
        row.provider_operation_started_at = connection.provider_operation_started_at
        row.payload = _model_json(connection)
        row.updated_at = connection.updated_at
        flag_modified(row, "payload")
        self.session.flush()


@dataclass(slots=True)
class AwsAuthorizationCleanupTombstoneRepository:
    session: Session

    def create(
        self,
        tombstone: AwsAuthorizationCleanupTombstone,
    ) -> AwsAuthorizationCleanupTombstone:
        row = AwsAuthorizationCleanupTombstoneTable(
            id=tombstone.id,
            user_id=tombstone.user_id,
            connection_id=tombstone.connection_id,
            account_id=tombstone.account_id,
            status=tombstone.status.value,
            provider_operation_id=tombstone.provider_operation_id,
            revision=tombstone.revision,
            next_reconcile_at=tombstone.next_reconcile_at,
            expires_at=tombstone.expires_at,
            claim_token=tombstone.claim_token,
            claim_expires_at=tombstone.claim_expires_at,
            reconcile_attempt_count=tombstone.reconcile_attempt_count,
            payload=_model_json(tombstone),
            created_at=tombstone.created_at,
            updated_at=tombstone.updated_at,
        )
        self.session.add(row)
        self.session.flush()
        return tombstone

    def claim_due(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> list[AwsAuthorizationCleanupTombstone]:
        statement = (
            select(AwsAuthorizationCleanupTombstoneTable)
            .where(
                AwsAuthorizationCleanupTombstoneTable.next_reconcile_at <= now,
                or_(
                    AwsAuthorizationCleanupTombstoneTable.claim_expires_at.is_(None),
                    AwsAuthorizationCleanupTombstoneTable.claim_expires_at <= now,
                ),
            )
            .order_by(
                AwsAuthorizationCleanupTombstoneTable.next_reconcile_at.asc(),
                AwsAuthorizationCleanupTombstoneTable.id.asc(),
            )
            .limit(max(limit, 1))
            .with_for_update(skip_locked=True)
        )
        claimed: list[AwsAuthorizationCleanupTombstone] = []
        for row in self.session.scalars(statement):
            current = AwsAuthorizationCleanupTombstone.model_validate(row.payload)
            tombstone = current.model_copy(
                update={
                    "claim_token": str(uuid4()),
                    "claim_expires_at": lease_until,
                    "updated_at": now,
                }
            )
            self._write(row, tombstone)
            claimed.append(tombstone)
        return claimed

    def finish_claim(
        self,
        claimed: AwsAuthorizationCleanupTombstone,
        updated: AwsAuthorizationCleanupTombstone,
    ) -> AwsAuthorizationCleanupTombstone | None:
        row = self._claimed_row(claimed)
        if row is None:
            return None
        finished = updated.model_copy(
            update={
                "revision": claimed.revision + 1,
                "claim_token": None,
                "claim_expires_at": None,
            }
        )
        self._write(row, finished)
        return finished

    def complete(self, claimed: AwsAuthorizationCleanupTombstone) -> bool:
        row = self._claimed_row(claimed)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def pending_count(self) -> int:
        return int(
            self.session.scalar(
                select(func.count()).select_from(AwsAuthorizationCleanupTombstoneTable)
            )
            or 0
        )

    def _claimed_row(
        self,
        claimed: AwsAuthorizationCleanupTombstone,
    ) -> AwsAuthorizationCleanupTombstoneTable | None:
        return _locked_claimed_row(
            self.session,
            AwsAuthorizationCleanupTombstoneTable,
            AwsAuthorizationCleanupTombstone.model_validate,
            claimed,
        )

    def _write(
        self,
        row: AwsAuthorizationCleanupTombstoneTable,
        tombstone: AwsAuthorizationCleanupTombstone,
    ) -> None:
        row.user_id = tombstone.user_id
        row.connection_id = tombstone.connection_id
        row.account_id = tombstone.account_id
        row.status = tombstone.status.value
        row.provider_operation_id = tombstone.provider_operation_id
        row.revision = tombstone.revision
        row.next_reconcile_at = tombstone.next_reconcile_at
        row.expires_at = tombstone.expires_at
        row.claim_token = tombstone.claim_token
        row.claim_expires_at = tombstone.claim_expires_at
        row.reconcile_attempt_count = tombstone.reconcile_attempt_count
        row.payload = _model_json(tombstone)
        row.updated_at = tombstone.updated_at
        flag_modified(row, "payload")
        self.session.flush()


def _unique_nonempty_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))
