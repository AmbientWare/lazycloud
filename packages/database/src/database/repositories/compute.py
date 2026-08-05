from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.repositories.common import (
    GlobalTableRepository,
    TableRepositoryConfig,
    WorkspaceTableRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.tables.compute import (
    AwsAccountConnectionTable,
    AwsAuthorizationCleanupTombstoneTable,
    ComputeCapacityOperationTable,
    ComputeCapacityRequestTable,
    ComputeJoinCredentialTable,
    ComputeLedgerTable,
    ComputeMachineEnrollmentTable,
    ComputePoolTable,
    ComputeProviderInstanceTable,
    ComputeSolverDecisionTable,
    ComputeSolverRunTable,
    TailnetCleanupTombstoneTable,
    WorkspaceComputePolicyTable,
)
from pydantic import BaseModel, Field, JsonValue, TypeAdapter
from shared.aws_connections import (
    AwsAccountConnection,
    AwsAuthorizationCleanupTombstone,
)
from shared.capacity import TERMINAL_REASON_MAX_LENGTH, CapacityFailureCode
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputeCredentialStatus,
    ComputeMachineEnrollmentStatus,
    ComputePreflightCheck,
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
    MachineReadinessPhase,
    TailnetCleanupTombstone,
    TailnetEnrollmentPhase,
)
from shared.compute_policy import (
    ComputePoolPhase,
    ComputePoolProviderState,
    ComputePoolRecord,
    ComputePoolVisibility,
    WorkspaceComputePolicy,
)
from shared.contracts import ContractModel
from shared.errors import ConflictError
from shared.identity import WorkspaceStatus
from shared.timestamps import utc_now
from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

type DatabaseInsertValue = JsonValue | datetime

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def _model_json(model: BaseModel) -> dict[str, JsonValue]:
    return _JSON_OBJECT_ADAPTER.validate_json(model.model_dump_json())


class ComputeCapacityRequestRecord(ContractModel):
    id: str
    workspace_id: str
    pool_id: str | None = None
    stub_id: str | None = None
    source: str
    max_spend_micros: int = 0
    ttl_seconds: int = 0
    status: str = "active"
    expires_at: datetime | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


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


class ComputeProviderInstanceRecord(ContractModel):
    id: str
    provider: str
    offer_id: str
    status: str
    source: str
    pool_id: str | None = None
    capacity_request_id: str | None = None
    instance_type: str | None = None
    instance_id: str | None = None
    machine_id: str | None = None
    gpu: str | None = None
    gpu_count: int = 0
    cpu_millicores: int = 0
    memory_mb: int = 0
    hourly_cost_micros: int = 0
    committed_micros: int = 0
    expires_at: datetime | None = None
    billing_renewal_at: datetime | None = None
    bootstrap_phase: MachineBootstrapPhase = MachineBootstrapPhase.Requested
    bootstrap_failure_reason: MachineBootstrapFailureReason | None = None
    bootstrap_failure_detail: str = ""
    bootstrap_observed_at: datetime = Field(default_factory=utc_now)
    launch_attempt: int = Field(default=1, ge=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ComputeSolverRunRecord(ContractModel):
    id: str
    workspace_id: str | None = None
    pool_id: str | None = None
    feasible: bool = False
    reason: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ComputeSolverDecisionRecord(ContractModel):
    id: str
    solver_run_id: str | None = None
    action: str
    provider: str | None = None
    offer_id: str | None = None
    reservation_id: str | None = None
    count: int = 0
    cost_micros: int = 0
    reason: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ComputeLedgerRecord(ContractModel):
    id: str
    source: str
    amount_micros: int
    started_at: datetime
    ended_at: datetime
    workspace_id: str | None = None
    pool_id: str | None = None
    reservation_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ComputeJoinCredentialRecord(ContractModel):
    id: str
    token_hash: str
    workspace_id: str
    pool_name: str
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
        return ComputeJoinCredentialRecord(
            id=self.id,
            token_hash=self.token_hash,
            workspace_id=self.workspace_id,
            pool_name=self.pool_name,
            machine_id=self.machine_id,
            created_by_token_id=self.created_by_token_id,
            status=ComputeCredentialStatus.Revoked,
            max_uses=self.max_uses,
            use_count=self.use_count,
            expires_at=self.expires_at,
            revoked_at=now,
            created_at=self.created_at,
            updated_at=now,
        )

    def with_use_count(self, use_count: int, *, now: datetime) -> ComputeJoinCredentialRecord:
        return ComputeJoinCredentialRecord(
            id=self.id,
            token_hash=self.token_hash,
            workspace_id=self.workspace_id,
            pool_name=self.pool_name,
            machine_id=self.machine_id,
            created_by_token_id=self.created_by_token_id,
            status=self.status,
            max_uses=self.max_uses,
            use_count=use_count,
            expires_at=self.expires_at,
            revoked_at=self.revoked_at,
            created_at=self.created_at,
            updated_at=now,
        )


class ComputeMachineEnrollmentRecord(ContractModel):
    id: str
    workspace_id: str
    pool_name: str
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
    tailnet_generation: int = Field(default=0, ge=0)
    tailnet_phase: TailnetEnrollmentPhase = TailnetEnrollmentPhase.Unconfigured
    tailnet_auth_key_id: str = ""
    tailnet_auth_key_expires_at: datetime | None = None
    tailnet_device_id: str = ""
    tailnet_hostname: str = ""
    tailnet_ips: list[str] = Field(default_factory=list)
    tailnet_verified_at: datetime | None = None
    tailnet_cleanup_auth_key_ids: list[str] = Field(default_factory=list)
    tailnet_cleanup_device_ids: list[str] = Field(default_factory=list)
    last_join_at: datetime
    last_heartbeat_at: datetime | None = None
    last_disconnect_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ComputeMachineEnrollmentCreate(ContractModel):
    workspace_id: str
    pool_name: str
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
    tailnet_generation: int = Field(default=0, ge=0)
    tailnet_phase: TailnetEnrollmentPhase = TailnetEnrollmentPhase.Unconfigured
    tailnet_auth_key_id: str = ""
    tailnet_auth_key_expires_at: datetime | None = None
    tailnet_device_id: str = ""
    tailnet_hostname: str = ""
    tailnet_ips: list[str] = Field(default_factory=list)
    tailnet_verified_at: datetime | None = None
    tailnet_cleanup_auth_key_ids: list[str] = Field(default_factory=list)
    tailnet_cleanup_device_ids: list[str] = Field(default_factory=list)
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
        return ComputeMachineEnrollmentRecord(
            id=existing.id,
            workspace_id=self.workspace_id,
            pool_name=self.pool_name,
            machine_id=self.machine_id,
            machine_fingerprint_hash=self.machine_fingerprint_hash,
            join_credential_id=self.join_credential_id,
            credential_hash=self.credential_hash,
            credential_generation=self.credential_generation,
            status=self.status,
            preflight_passed=self.preflight_passed,
            heartbeat_confirmed=self.heartbeat_confirmed,
            schedulable=self.schedulable,
            capacity_state=self.capacity_state,
            capacity_reason=self.capacity_reason,
            capacity_observed_at=self.capacity_observed_at,
            capacity_notice_at=self.capacity_notice_at,
            readiness_phase=self.readiness_phase,
            hostname=self.hostname,
            os=self.os,
            arch=self.arch,
            cpu_count=self.cpu_count,
            cpu_millicores=self.cpu_millicores,
            memory_mb=self.memory_mb,
            gpus=self.gpus,
            gpu_ids=self.gpu_ids,
            gpu_count=self.gpu_count,
            executor=self.executor,
            preflight=self.preflight,
            agent_version=self.agent_version,
            tailnet_generation=existing.tailnet_generation,
            tailnet_phase=existing.tailnet_phase,
            tailnet_auth_key_id=existing.tailnet_auth_key_id,
            tailnet_auth_key_expires_at=existing.tailnet_auth_key_expires_at,
            tailnet_device_id=existing.tailnet_device_id,
            tailnet_hostname=existing.tailnet_hostname,
            tailnet_ips=existing.tailnet_ips,
            tailnet_verified_at=existing.tailnet_verified_at,
            tailnet_cleanup_auth_key_ids=existing.tailnet_cleanup_auth_key_ids,
            tailnet_cleanup_device_ids=existing.tailnet_cleanup_device_ids,
            last_join_at=self.last_join_at,
            last_heartbeat_at=self.last_heartbeat_at,
            last_disconnect_at=self.last_disconnect_at,
            revoked_at=self.revoked_at,
            created_at=existing.created_at,
            updated_at=updated_at,
        )


@dataclass(slots=True)
class ComputePoolRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ComputePoolRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(ComputePoolTable, ComputePoolRecord),
        )

    def upsert(self, record: ComputePoolRecord) -> ComputePoolRecord:
        # The immutability comparison below is by identity, and it runs before the
        # store's own validation, so the record has to be typed by the time it
        # gets there or an unchanged owner reads as a changed one. `dict(record)`
        # rather than `model_dump`: dumping serializes, and a drifted record would
        # raise the serializer warning here instead of where it was introduced.
        record = ComputePoolRecord.model_validate(dict(record))
        current = self.get(record.id, for_update=True)
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
    ) -> ComputePoolRecord | None:
        statement = select(ComputePoolTable).where(
            ComputePoolTable.workspace_id == workspace_id,
            ComputePoolTable.name == name,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).one_or_none()
        return ComputePoolRecord.model_validate(row.payload) if row is not None else None

    def get_by_capacity_owner_id(
        self,
        capacity_owner_id: str,
        *,
        for_update: bool = False,
    ) -> ComputePoolRecord | None:
        statement = select(ComputePoolTable).where(
            ComputePoolTable.capacity_owner_id == capacity_owner_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).one_or_none()
        return ComputePoolRecord.model_validate(row.payload) if row is not None else None

    def delete_for_workspace_deletion(self, pool_id: str, *, workspace_id: str) -> bool:
        workspace = WorkspaceRepository(self.session).lock_for_deletion(workspace_id)
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
        result = self.session.execute(
            delete(ComputePoolTable).where(
                ComputePoolTable.id == pool_id,
                ComputePoolTable.workspace_id == workspace_id,
            )
        )
        self.session.flush()
        return isinstance(result, CursorResult) and result.rowcount > 0

    def get(self, pool_id: str, *, for_update: bool = False) -> ComputePoolRecord | None:
        """System lookup by pool id for placement/capacity reconciliation."""
        statement = select(ComputePoolTable).where(ComputePoolTable.id == pool_id)
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return ComputePoolRecord.model_validate(row.payload) if row is not None else None

    def get_by_identity(
        self,
        *,
        workspace_id: str,
        provider_ref: str,
        region: str,
        capability_key: str,
        root_volume_gib: int,
        for_update: bool = False,
    ) -> ComputePoolRecord | None:
        """Look up a provisioning unit by everything AWS pins to one ASG.

        `root_volume_gib` belongs to the identity because it feeds the launch
        template: two callers disagreeing on it for one capability key would
        alternate the template version on every reconcile and no node would
        ever settle.
        """
        statement = select(ComputePoolTable).where(
            ComputePoolTable.workspace_id == workspace_id,
            ComputePoolTable.provider_ref == provider_ref,
            ComputePoolTable.region == region,
            ComputePoolTable.capability_key == capability_key,
            ComputePoolTable.root_volume_gib == root_volume_gib,
            ComputePoolTable.visibility == ComputePoolVisibility.Internal.value,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return ComputePoolRecord.model_validate(row.payload) if row is not None else None

    def list_for_machine_pool(
        self,
        workspace_id: str,
        machine_pool: str,
    ) -> list[ComputePoolRecord]:
        """Every unit feeding one scheduling group, best candidate first."""
        statement = (
            select(ComputePoolTable)
            .where(
                ComputePoolTable.workspace_id == workspace_id,
                ComputePoolTable.machine_pool == machine_pool,
            )
            .order_by(ComputePoolTable.priority.desc(), ComputePoolTable.id)
        )
        return [
            ComputePoolRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

    def list_for_workspace(self, workspace_id: str) -> list[ComputePoolRecord]:
        statement = (
            select(ComputePoolTable)
            .where(ComputePoolTable.workspace_id == workspace_id)
            .order_by(ComputePoolTable.created_at, ComputePoolTable.id)
        )
        return [
            ComputePoolRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

    def list_across_workspaces(self) -> list[ComputePoolRecord]:
        """System listing every unit, for scheduler controller construction."""
        statement = select(ComputePoolTable).order_by(
            ComputePoolTable.workspace_id,
            ComputePoolTable.id,
        )
        return [
            ComputePoolRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

    def list_internal(self, *, workspace_id: str) -> list[ComputePoolRecord]:
        return self._list_internal(workspace_id=workspace_id)

    def list_internal_across_workspaces(self) -> list[ComputePoolRecord]:
        """System listing over every workspace's internal placement pools."""
        return self._list_internal(workspace_id=None)

    def _list_internal(self, *, workspace_id: str | None) -> list[ComputePoolRecord]:
        statement = select(ComputePoolTable).where(
            ComputePoolTable.visibility == ComputePoolVisibility.Internal.value
        )
        if workspace_id is not None:
            statement = statement.where(ComputePoolTable.workspace_id == workspace_id)
        statement = statement.order_by(ComputePoolTable.updated_at, ComputePoolTable.id)
        return [
            ComputePoolRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

    def list_for_provider_connection(self, connection_id: str) -> list[ComputePoolRecord]:
        statement = (
            select(ComputePoolTable)
            .where(ComputePoolTable.provider_connection_id == connection_id)
            .order_by(ComputePoolTable.created_at, ComputePoolTable.id)
        )
        return [
            ComputePoolRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

    def update_capacity(
        self,
        pool_id: str,
        *,
        expected_generation: int,
        desired_machines: int,
        max_machines: int,
        observed_machines: int,
        phase: ComputePoolPhase,
        provider_state: ComputePoolProviderState,
    ) -> ComputePoolRecord | None:
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
            }
        )
        return self.upsert(updated)

    def apply_provider_state(
        self,
        pool_id: str,
        *,
        generation: int,
        observed_machines: int,
        phase: ComputePoolPhase,
        provider_state: ComputePoolProviderState,
    ) -> ComputePoolRecord | None:
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

    def _write_columns(self, record: ComputePoolRecord) -> None:
        row = self.session.scalars(
            select(ComputePoolTable).where(ComputePoolTable.id == record.id).with_for_update()
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
        row.machine_pool = record.machine_pool
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
        row.workspace_machine_limit = record.workspace_machine_limit
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
            "default_placement": policy.default_placement.value,
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
        row.default_placement = policy.default_placement.value
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

    def list_for_owner(self, capacity_owner_id: str) -> list[ComputeCapacityOperationRecord]:
        rows = self.session.scalars(
            select(ComputeCapacityOperationTable)
            .where(ComputeCapacityOperationTable.capacity_owner_id == capacity_owner_id)
            .order_by(ComputeCapacityOperationTable.created_at, ComputeCapacityOperationTable.id)
        )
        return [ComputeCapacityOperationRecord.model_validate(row.payload) for row in rows]

    def peak_desired_unit(self, capacity_owner_id: str) -> int:
        """Highest unit this owner has ever been driven to, released rows included.

        Released operations stay readable precisely so this stays monotonic: it
        is the durable answer to "has the pool ever reached its initial size",
        which is what stops the sizer from buying back every machine the drain
        controller retires.
        """
        highest = self.session.scalar(
            select(func.max(ComputeCapacityOperationTable.desired_unit)).where(
                ComputeCapacityOperationTable.capacity_owner_id == capacity_owner_id
            )
        )
        return int(highest or 0)

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


@dataclass(slots=True)
class ComputeCapacityRequestRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ComputeCapacityRequestRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(ComputeCapacityRequestTable, ComputeCapacityRequestRecord),
        )

    def upsert(self, record: ComputeCapacityRequestRecord) -> ComputeCapacityRequestRecord:
        return self.records.upsert(record, workspace_id=record.workspace_id, status=record.status)

    def list_for_pool(self, pool_id: str) -> list[ComputeCapacityRequestRecord]:
        """System listing keyed by an already-authorized pool id."""
        return [item for item in self.records.list_across_workspaces() if item.pool_id == pool_id]

    def active_for_pool(
        self,
        pool_id: str,
        *,
        for_update: bool = False,
    ) -> ComputeCapacityRequestRecord | None:
        statement = select(ComputeCapacityRequestTable).where(
            ComputeCapacityRequestTable.pool_id == pool_id,
            ComputeCapacityRequestTable.status == "active",
        )
        if for_update:
            statement = statement.with_for_update()
        rows = list(self.session.scalars(statement))
        if len(rows) > 1:
            raise RuntimeError(f"pool {pool_id} has multiple active capacity requests")
        return ComputeCapacityRequestRecord.model_validate(rows[0].payload) if rows else None


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
    ) -> list[ComputeProviderInstanceRecord]:
        statement = (
            select(ComputeProviderInstanceTable)
            .where(ComputeProviderInstanceTable.pool_id == pool_id)
            .order_by(
                ComputeProviderInstanceTable.created_at.desc(),
                ComputeProviderInstanceTable.id.asc(),
            )
        )
        if for_update:
            statement = statement.with_for_update()
        return [
            ComputeProviderInstanceRecord.model_validate(row.payload)
            for row in self.session.scalars(statement)
        ]

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
        current = ComputeProviderInstanceRecord.model_validate(row.payload)
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
        current = ComputeProviderInstanceRecord.model_validate(row.payload)
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
        return (
            ComputeProviderInstanceRecord.model_validate(row.payload) if row is not None else None
        )

    def get_by_machine(self, machine_id: str) -> ComputeProviderInstanceRecord | None:
        row = self.session.scalars(
            select(ComputeProviderInstanceTable).where(
                ComputeProviderInstanceTable.machine_id == machine_id
            )
        ).one_or_none()
        return (
            ComputeProviderInstanceRecord.model_validate(row.payload) if row is not None else None
        )

    def list_open(self) -> list[ComputeProviderInstanceRecord]:
        closed = {"deleted", "failed"}
        return [item for item in self.records.list() if item.status not in closed]


@dataclass(slots=True)
class ComputeSolverRunRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ComputeSolverRunRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(ComputeSolverRunTable, ComputeSolverRunRecord),
        )

    def upsert(self, record: ComputeSolverRunRecord) -> ComputeSolverRunRecord:
        """System-authority write; cluster-wide solver runs carry no workspace."""
        return self.records.upsert_across_workspaces(record, workspace_id=record.workspace_id)


@dataclass(slots=True)
class ComputeSolverDecisionRepository:
    session: Session

    @property
    def records(self) -> GlobalTableRepository[ComputeSolverDecisionRecord]:
        return GlobalTableRepository(
            self.session,
            TableRepositoryConfig(ComputeSolverDecisionTable, ComputeSolverDecisionRecord),
        )

    def upsert(self, record: ComputeSolverDecisionRecord) -> ComputeSolverDecisionRecord:
        return self.records.upsert(record, status=record.action)


@dataclass(slots=True)
class ComputeLedgerRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ComputeLedgerRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(ComputeLedgerTable, ComputeLedgerRecord),
        )

    def append(self, record: ComputeLedgerRecord) -> ComputeLedgerRecord:
        """System-authority write; platform-level ledger rows carry no workspace."""
        return self.records.upsert_across_workspaces(
            record,
            workspace_id=record.workspace_id,
            status=record.source,
        )

    def append_for_workspace_deletion(
        self,
        record: ComputeLedgerRecord,
    ) -> ComputeLedgerRecord:
        if record.workspace_id is None:
            raise ValueError("workspace deletion ledger row requires workspace ownership")
        workspace = WorkspaceRepository(self.session).lock_for_deletion(record.workspace_id)
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(f"workspace cleanup requires deleting state: {record.workspace_id}")
        row = self.session.get(ComputeLedgerTable, record.id)
        if row is None:
            row = ComputeLedgerTable(id=record.id)
            self.session.add(row)
        row.workspace_id = record.workspace_id
        row.pool_id = record.pool_id or None
        row.reservation_id = record.reservation_id or None
        row.source = record.source
        row.amount_micros = record.amount_micros
        row.started_at = record.started_at
        row.ended_at = record.ended_at
        row.payload = _model_json(record)
        flag_modified(row, "payload")
        self.session.flush()
        return record

    def list_for_workspace(self, workspace_id: str) -> list[ComputeLedgerRecord]:
        return [
            item
            for item in self.records.list(workspace_id=workspace_id)
            if item.workspace_id == workspace_id
        ]


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
        workspace_id: str,
        pool_name: str,
        machine_id: str = "",
        created_by_token_id: str | None,
        max_uses: int,
        expires_at: datetime,
    ) -> ComputeJoinCredentialRecord:
        return self.records.create(
            {
                "token_hash": token_hash,
                "workspace_id": workspace_id,
                "pool_name": pool_name,
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

    def lock_pool(self, workspace_id: str, pool_name: str) -> bool:
        """Fence the unit a credential is minted against for the mint's duration."""
        statement = (
            select(ComputePoolTable.id)
            .where(
                ComputePoolTable.workspace_id == workspace_id,
                ComputePoolTable.name == pool_name,
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

    def list_for_pool(
        self,
        workspace_id: str,
        pool_name: str,
        *,
        for_update: bool = False,
    ) -> list[ComputeJoinCredentialRecord]:
        statement = select(ComputeJoinCredentialTable).where(
            ComputeJoinCredentialTable.workspace_id == workspace_id,
            ComputeJoinCredentialTable.pool_name == pool_name,
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

    def delete_for_pool(self, workspace_id: str, pool_name: str) -> int:
        ids = list(
            self.session.scalars(
                select(ComputeJoinCredentialTable.id).where(
                    ComputeJoinCredentialTable.workspace_id == workspace_id,
                    ComputeJoinCredentialTable.pool_name == pool_name,
                )
            )
        )
        self.session.execute(
            delete(ComputeJoinCredentialTable).where(
                ComputeJoinCredentialTable.workspace_id == workspace_id,
                ComputeJoinCredentialTable.pool_name == pool_name,
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
        workspace_id: str,
        pool_name: str,
        machine_fingerprint_hash: str,
        *,
        for_update: bool = False,
    ) -> ComputeMachineEnrollmentRecord | None:
        return self._one(
            select(ComputeMachineEnrollmentTable).where(
                ComputeMachineEnrollmentTable.workspace_id == workspace_id,
                ComputeMachineEnrollmentTable.pool_name == pool_name,
                ComputeMachineEnrollmentTable.machine_fingerprint_hash == machine_fingerprint_hash,
            ),
            for_update=for_update,
        )

    def by_machine(
        self,
        workspace_id: str,
        machine_id: str,
        *,
        pool_name: str = "",
        for_update: bool = False,
    ) -> ComputeMachineEnrollmentRecord | None:
        statement = select(ComputeMachineEnrollmentTable).where(
            ComputeMachineEnrollmentTable.workspace_id == workspace_id,
            ComputeMachineEnrollmentTable.machine_id == machine_id,
        )
        if pool_name:
            statement = statement.where(ComputeMachineEnrollmentTable.pool_name == pool_name)
        return self._one(statement, for_update=for_update)

    def list_for_pool(
        self,
        workspace_id: str,
        pool_name: str,
    ) -> list[ComputeMachineEnrollmentRecord]:
        statement = (
            select(ComputeMachineEnrollmentTable)
            .where(
                ComputeMachineEnrollmentTable.workspace_id == workspace_id,
                ComputeMachineEnrollmentTable.pool_name == pool_name,
            )
            .order_by(ComputeMachineEnrollmentTable.created_at.asc())
        )
        return [
            ComputeMachineEnrollmentRecord.model_validate(row.payload)
            for row in self.session.scalars(statement)
        ]

    def delete_for_pool(self, workspace_id: str, pool_name: str) -> int:
        ids = list(
            self.session.scalars(
                select(ComputeMachineEnrollmentTable.id).where(
                    ComputeMachineEnrollmentTable.workspace_id == workspace_id,
                    ComputeMachineEnrollmentTable.pool_name == pool_name,
                )
            )
        )
        self.session.execute(
            delete(ComputeMachineEnrollmentTable).where(
                ComputeMachineEnrollmentTable.workspace_id == workspace_id,
                ComputeMachineEnrollmentTable.pool_name == pool_name,
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
        return (
            ComputeMachineEnrollmentRecord.model_validate(row.payload) if row is not None else None
        )


@dataclass(slots=True)
class TailnetCleanupTombstoneRepository:
    session: Session

    def schedule(
        self,
        *,
        workspace_id: str,
        pool_name: str,
        machine_id: str,
        generations: list[int],
        auth_key_ids: list[str],
        device_ids: list[str],
        not_before: datetime,
        now: datetime,
    ) -> TailnetCleanupTombstone:
        candidate = TailnetCleanupTombstone(
            id=str(uuid4()),
            workspace_id=workspace_id,
            pool_name=pool_name,
            machine_id=machine_id,
            generations=_unique_positive_integers(generations),
            auth_key_ids=_unique_nonempty_strings(auth_key_ids),
            device_ids=_unique_nonempty_strings(device_ids),
            not_before=not_before,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        if self._insert_if_absent(candidate):
            return candidate

        row = self.session.scalars(
            select(TailnetCleanupTombstoneTable)
            .where(TailnetCleanupTombstoneTable.machine_id == machine_id)
            .with_for_update()
        ).one()

        current = TailnetCleanupTombstone.model_validate(row.payload)
        tombstone = current.model_copy(
            update={
                "workspace_id": workspace_id,
                "pool_name": pool_name,
                "generations": _unique_positive_integers(
                    [*current.generations, *candidate.generations]
                ),
                "auth_key_ids": _unique_nonempty_strings(
                    [*current.auth_key_ids, *candidate.auth_key_ids]
                ),
                "device_ids": _unique_nonempty_strings(
                    [*current.device_ids, *candidate.device_ids]
                ),
                "not_before": max(current.not_before, not_before),
                "next_attempt_at": min(current.next_attempt_at, now),
                "revision": current.revision + 1,
                "last_error": "",
                "claim_token": "",
                "claimed_until": None,
                "updated_at": now,
            }
        )
        self._write(row, tombstone)
        return tombstone

    def _insert_if_absent(self, tombstone: TailnetCleanupTombstone) -> bool:
        values: dict[str, DatabaseInsertValue] = {
            "id": tombstone.id,
            "machine_id": tombstone.machine_id,
            "next_attempt_at": tombstone.next_attempt_at,
            "claimed_until": None,
            "payload": _model_json(tombstone),
            "created_at": tombstone.created_at,
            "updated_at": tombstone.updated_at,
        }
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = (
                postgresql_insert(TailnetCleanupTombstoneTable)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[TailnetCleanupTombstoneTable.machine_id])
                .returning(TailnetCleanupTombstoneTable.id)
            )
        elif dialect == "sqlite":
            statement = (
                sqlite_insert(TailnetCleanupTombstoneTable)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[TailnetCleanupTombstoneTable.machine_id])
                .returning(TailnetCleanupTombstoneTable.id)
            )
        else:
            raise RuntimeError(f"unsupported tailnet cleanup database dialect: {dialect}")
        return self.session.scalar(statement) is not None

    def claim_due(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> list[TailnetCleanupTombstone]:
        statement = (
            select(TailnetCleanupTombstoneTable)
            .where(
                TailnetCleanupTombstoneTable.next_attempt_at <= now,
                or_(
                    TailnetCleanupTombstoneTable.claimed_until.is_(None),
                    TailnetCleanupTombstoneTable.claimed_until <= now,
                ),
            )
            .order_by(
                TailnetCleanupTombstoneTable.next_attempt_at.asc(),
                TailnetCleanupTombstoneTable.created_at.asc(),
            )
            .limit(max(limit, 1))
            .with_for_update(skip_locked=True)
        )
        claimed: list[TailnetCleanupTombstone] = []
        for row in self.session.scalars(statement):
            current = TailnetCleanupTombstone.model_validate(row.payload)
            tombstone = current.model_copy(
                update={
                    "claim_token": str(uuid4()),
                    "claimed_until": lease_until,
                    "updated_at": now,
                }
            )
            self._write(row, tombstone)
            claimed.append(tombstone)
        return claimed

    def claim_machine(
        self,
        machine_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> TailnetCleanupTombstone | None:
        row = self.session.scalars(
            select(TailnetCleanupTombstoneTable)
            .where(
                TailnetCleanupTombstoneTable.machine_id == machine_id,
                TailnetCleanupTombstoneTable.next_attempt_at <= now,
                or_(
                    TailnetCleanupTombstoneTable.claimed_until.is_(None),
                    TailnetCleanupTombstoneTable.claimed_until <= now,
                ),
            )
            .with_for_update()
        ).first()
        if row is None:
            return None
        current = TailnetCleanupTombstone.model_validate(row.payload)
        tombstone = current.model_copy(
            update={
                "claim_token": str(uuid4()),
                "claimed_until": lease_until,
                "updated_at": now,
            }
        )
        self._write(row, tombstone)
        return tombstone

    def complete(self, tombstone: TailnetCleanupTombstone) -> bool:
        row = self._claimed_row(tombstone)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def reschedule(
        self,
        tombstone: TailnetCleanupTombstone,
        *,
        next_attempt_at: datetime,
        last_error: str,
        now: datetime,
    ) -> bool:
        row = self._claimed_row(tombstone)
        if row is None:
            return False
        current = TailnetCleanupTombstone.model_validate(row.payload)
        updated = current.model_copy(
            update={
                "next_attempt_at": next_attempt_at,
                "attempt_count": current.attempt_count + 1,
                "last_error": last_error,
                "claim_token": "",
                "claimed_until": None,
                "updated_at": now,
            }
        )
        self._write(row, updated)
        return True

    def get_by_machine(self, machine_id: str) -> TailnetCleanupTombstone | None:
        row = self.session.scalars(
            select(TailnetCleanupTombstoneTable).where(
                TailnetCleanupTombstoneTable.machine_id == machine_id
            )
        ).first()
        return TailnetCleanupTombstone.model_validate(row.payload) if row is not None else None

    def pending_count(self) -> int:
        return int(
            self.session.scalar(select(func.count()).select_from(TailnetCleanupTombstoneTable)) or 0
        )

    def _claimed_row(
        self,
        tombstone: TailnetCleanupTombstone,
    ) -> TailnetCleanupTombstoneTable | None:
        row = self.session.scalars(
            select(TailnetCleanupTombstoneTable)
            .where(TailnetCleanupTombstoneTable.id == tombstone.id)
            .with_for_update()
        ).first()
        if row is None:
            return None
        current = TailnetCleanupTombstone.model_validate(row.payload)
        if current.revision != tombstone.revision or current.claim_token != tombstone.claim_token:
            return None
        return row

    def _write(
        self,
        row: TailnetCleanupTombstoneTable,
        tombstone: TailnetCleanupTombstone,
    ) -> None:
        row.payload = _model_json(tombstone)
        row.machine_id = tombstone.machine_id
        row.next_attempt_at = tombstone.next_attempt_at
        row.claimed_until = tombstone.claimed_until
        row.updated_at = tombstone.updated_at
        flag_modified(row, "payload")
        self.session.flush()


@dataclass(slots=True)
class AwsAccountConnectionRepository:
    session: Session

    def create(self, connection: AwsAccountConnection) -> AwsAccountConnection:
        row = AwsAccountConnectionTable(
            id=connection.id,
            workspace_id=connection.workspace_id,
            account_id=connection.account_id,
            external_id=connection.external_id,
            machine_pool=connection.machine_pool,
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

    def get_for_account(
        self,
        workspace_id: str,
        account_id: str,
        *,
        for_update: bool = False,
    ) -> AwsAccountConnection | None:
        statement = select(AwsAccountConnectionTable).where(
            AwsAccountConnectionTable.workspace_id == workspace_id,
            AwsAccountConnectionTable.account_id == account_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return AwsAccountConnection.model_validate(row.payload) if row is not None else None

    def get_for_workspace(
        self,
        workspace_id: str,
        *,
        for_update: bool = False,
    ) -> AwsAccountConnection | None:
        statement = select(AwsAccountConnectionTable).where(
            AwsAccountConnectionTable.workspace_id == workspace_id
        )
        if for_update:
            statement = statement.with_for_update()
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

    def list_for_workspace(self, workspace_id: str) -> list[AwsAccountConnection]:
        statement = (
            select(AwsAccountConnectionTable)
            .where(AwsAccountConnectionTable.workspace_id == workspace_id)
            .order_by(
                AwsAccountConnectionTable.created_at.desc(),
                AwsAccountConnectionTable.id.asc(),
            )
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
        row = self.session.scalars(
            select(AwsAccountConnectionTable)
            .where(AwsAccountConnectionTable.id == claimed.id)
            .with_for_update()
        ).first()
        if row is None:
            return None
        current = AwsAccountConnection.model_validate(row.payload)
        if current.revision != claimed.revision or current.claim_token != claimed.claim_token:
            return None
        return row

    def _write(
        self,
        row: AwsAccountConnectionTable,
        connection: AwsAccountConnection,
    ) -> None:
        row.workspace_id = connection.workspace_id
        row.account_id = connection.account_id
        row.external_id = connection.external_id
        row.machine_pool = connection.machine_pool
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

    def dependent_pool_count(self, connection_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(ComputePoolTable)
                .where(
                    ComputePoolTable.provider_connection_id == connection_id,
                    ComputePoolTable.phase != ComputePoolPhase.Deleted.value,
                )
            )
            or 0
        )


@dataclass(slots=True)
class AwsAuthorizationCleanupTombstoneRepository:
    session: Session

    def create(
        self,
        tombstone: AwsAuthorizationCleanupTombstone,
    ) -> AwsAuthorizationCleanupTombstone:
        row = AwsAuthorizationCleanupTombstoneTable(
            id=tombstone.id,
            workspace_id=tombstone.workspace_id,
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
        row = self.session.scalars(
            select(AwsAuthorizationCleanupTombstoneTable)
            .where(AwsAuthorizationCleanupTombstoneTable.id == claimed.id)
            .with_for_update()
        ).first()
        if row is None:
            return None
        current = AwsAuthorizationCleanupTombstone.model_validate(row.payload)
        if current.revision != claimed.revision or current.claim_token != claimed.claim_token:
            return None
        return row

    def _write(
        self,
        row: AwsAuthorizationCleanupTombstoneTable,
        tombstone: AwsAuthorizationCleanupTombstone,
    ) -> None:
        row.workspace_id = tombstone.workspace_id
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


def _unique_positive_integers(values: list[int]) -> list[int]:
    if any(value < 1 for value in values):
        raise ValueError("tailnet cleanup generations must be positive")
    return list(dict.fromkeys(values))
