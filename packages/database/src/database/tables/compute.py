from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdPayloadTable, json_type, uuid_type


class ComputeUnitTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "compute_units"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("workspace_id", "name", name="uq_compute_units_workspace_name"),
        UniqueConstraint("capacity_owner_id", name="uq_compute_units_capacity_owner_id"),
        Index("ix_compute_units_workspace_pool", "workspace_id", "pool"),
        Index(
            "uq_compute_units_internal_placement",
            "workspace_id",
            "provider_ref",
            "region",
            "capability_key",
            "root_volume_gib",
            unique=True,
            postgresql_where=text("visibility = 'internal' AND provider_ref <> ''"),
            sqlite_where=text("visibility = 'internal' AND provider_ref <> ''"),
        ),
        CheckConstraint(
            "min_machines >= 0 AND desired_machines >= min_machines "
            "AND max_machines >= desired_machines AND observed_machines >= 0 "
            "AND initial_machines >= min_machines AND max_machines >= initial_machines",
            name="ck_compute_units_machine_capacity",
        ),
        CheckConstraint("generation > 0", name="ck_compute_units_generation"),
        CheckConstraint(
            "min_free_cpu_millicores >= 0 AND min_free_memory_mib >= 0 "
            "AND min_free_gpu_count >= 0 AND worker_cpu_millicores >= 0 "
            "AND worker_memory_mib >= 0 AND worker_gpu_count >= 0",
            name="ck_compute_units_worker_shape",
        ),
        CheckConstraint(
            "(worker_gpu_type = '' AND worker_gpu_count = 0) "
            "OR (worker_gpu_type <> '' AND worker_gpu_count > 0)",
            name="ck_compute_units_worker_gpu",
        ),
        CheckConstraint(
            "idle_drain_timeout_seconds BETWEEN 60 AND 86400 "
            "AND scale_up_cooldown_seconds BETWEEN 0 AND 86400 "
            "AND scale_down_cooldown_seconds BETWEEN 0 AND 86400 "
            "AND registration_timeout_seconds BETWEEN 30 AND 3600",
            name="ck_compute_units_lifecycle_timeouts",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    capacity_owner_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    capacity_owner_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    capacity_owner_source: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    pool: Mapped[str] = mapped_column(String(240), nullable=False)
    provider: Mapped[str] = mapped_column(String(120), nullable=False, default="local")
    selector: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(80), nullable=False, default="active")
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="autosolver")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_ref: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    provider_connection_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("aws_account_connections.id", ondelete="RESTRICT"),
        nullable=True,
    )
    capacity_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="direct")
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="public")
    region: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    offer_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    capability_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    desired_machines: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    initial_machines: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    min_machines: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    max_machines: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    observed_machines: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    provider_state: Mapped[dict[str, JsonValue]] = mapped_column(
        json_type,
        nullable=False,
        default=dict,
    )
    scaling_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    default_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_free_cpu_millicores: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_free_memory_mib: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_free_gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_cpu_millicores: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_memory_mib: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_gpu_type: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    worker_gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_runtimes: Mapped[list[str]] = mapped_column(json_type, nullable=False)
    worker_preemptible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idle_drain_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    scale_up_cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    scale_down_cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    registration_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    workspace_machine_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    root_volume_gib: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    transport: Mapped[str] = mapped_column(String(32), nullable=False, default="tsnet_restricted")
    fallback: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")


class WorkspaceComputePolicyTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "workspace_compute_policies"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint(
            "workspace_id",
            name="uq_workspace_compute_policies_workspace",
        ),
        CheckConstraint("revision > 0", name="ck_workspace_compute_policies_revision"),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    default_pool: Mapped[str] = mapped_column(
        String(240),
        nullable=False,
        default="lazycloud",
    )


class ComputeCapacityOperationTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "compute_capacity_operations"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint(
            "capacity_owner_id",
            "operation_id",
            name="uq_compute_capacity_operations_owner_operation",
        ),
        UniqueConstraint("reservation_id", name="uq_compute_capacity_operations_reservation"),
        Index("ix_compute_capacity_operations_owner_status", "capacity_owner_id", "status"),
        CheckConstraint("desired_unit > 0", name="ck_compute_capacity_operations_desired_unit"),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    pool_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("compute_units.id", ondelete="CASCADE"),
        nullable=False,
    )
    capacity_owner_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    reservation_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    operation_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    desired_unit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    target_machine_id: Mapped[str | None] = mapped_column(uuid_type, nullable=True)


class ComputeProviderInstanceTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "compute_provider_instances"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_compute_provider_instances_pool", "pool_id"),
        Index("ix_compute_provider_instances_renewal", "billing_renewal_at"),
        Index(
            "uq_compute_provider_instances_pool_instance",
            "pool_id",
            "instance_id",
            unique=True,
            postgresql_where=text("pool_id IS NOT NULL AND instance_id IS NOT NULL"),
            sqlite_where=text("pool_id IS NOT NULL AND instance_id IS NOT NULL"),
        ),
        Index(
            "uq_compute_provider_instances_machine",
            "machine_id",
            unique=True,
            postgresql_where=text("machine_id IS NOT NULL"),
            sqlite_where=text("machine_id IS NOT NULL"),
        ),
    )

    pool_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("compute_units.id", ondelete="CASCADE"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    offer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    instance_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    instance_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    machine_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("machines.id", ondelete="SET NULL"),
        nullable=True,
    )
    gpu: Mapped[str | None] = mapped_column(String(160), nullable=True)
    gpu_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cpu_millicores: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    memory_mb: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    hourly_cost_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    committed_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    billing_renewal_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class ComputeJoinCredentialTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "compute_join_credentials"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("token_hash", name="uq_compute_join_credentials_token_hash"),
        CheckConstraint(
            "max_uses > 0 AND use_count >= 0 AND use_count <= max_uses",
            name="ck_compute_join_credentials_use_count",
        ),
        Index(
            "ix_compute_join_credentials_workspace_owner_status",
            "workspace_id",
            "capacity_owner_id",
            "status",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    capacity_owner_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    pool: Mapped[str] = mapped_column(String(240), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_token_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    max_uses: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    use_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ComputeMachineEnrollmentTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "compute_machine_enrollments"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint(
            "workspace_id",
            "machine_id",
            name="uq_compute_machine_enrollments_machine",
        ),
        UniqueConstraint(
            "workspace_id",
            "machine_fingerprint_hash",
            name="uq_compute_machine_enrollments_fingerprint",
        ),
        UniqueConstraint(
            "credential_hash",
            name="uq_compute_machine_enrollments_credential_hash",
        ),
        CheckConstraint(
            "credential_generation > 0",
            name="ck_compute_machine_enrollments_generation",
        ),
        CheckConstraint(
            "capacity_state IN ('available', 'preempting', 'cordoned')",
            name="ck_compute_machine_enrollments_capacity_state",
        ),
        Index(
            "ix_compute_machine_enrollments_workspace_owner_status",
            "workspace_id",
            "capacity_owner_id",
            "status",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    capacity_owner_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    pool: Mapped[str] = mapped_column(String(240), nullable=False)
    machine_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("machines.id", ondelete="CASCADE"),
        nullable=False,
    )
    machine_fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    join_credential_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("compute_join_credentials.id", ondelete="SET NULL"),
        nullable=True,
    )
    credential_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    preflight_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    heartbeat_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    schedulable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capacity_state: Mapped[str] = mapped_column(String(32), nullable=False, default="available")
    capacity_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    readiness_phase: Mapped[str] = mapped_column(String(32), nullable=False, default="joining")
    last_join_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_disconnect_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TailnetCleanupTombstoneTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "tailnet_cleanup_tombstones"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("machine_id", name="uq_tailnet_cleanup_tombstones_machine"),
        Index(
            "ix_tailnet_cleanup_tombstones_due",
            "next_attempt_at",
            "claimed_until",
        ),
    )

    machine_id: Mapped[str] = mapped_column(String(80), nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AwsAccountConnectionTable(IdPayloadTable, DatabaseBase):
    """The customer AWS account backing every workspace one user owns.

    One per account rather than per workspace: an org running dev, staging, and prod
    authorized the same account once, and re-authorizing it per workspace produced
    three records that had to be kept in step by hand.
    """

    __tablename__ = "aws_account_connections"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("user_id", name="uq_aws_account_connections_user"),
        UniqueConstraint("external_id", name="uq_aws_account_connections_external_id"),
        Index(
            "ix_aws_account_connections_reconcile_due",
            "next_reconcile_at",
            "claim_expires_at",
        ),
        CheckConstraint("revision > 0", name="ck_aws_account_connections_revision"),
        CheckConstraint(
            "reconcile_attempt_count >= 0",
            name="ck_aws_account_connections_reconcile_attempts",
        ),
    )

    user_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[str] = mapped_column(String(12), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    pool: Mapped[str] = mapped_column(String(240), nullable=False, default="aws")
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    next_reconcile_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconcile_attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    provider_operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_operation_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AwsAuthorizationCleanupTombstoneTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "aws_authorization_cleanup_tombstones"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint(
            "provider_operation_id",
            name="uq_aws_authorization_cleanup_operation",
        ),
        Index(
            "ix_aws_authorization_cleanup_due",
            "next_reconcile_at",
            "claim_expires_at",
        ),
        CheckConstraint("revision > 0", name="ck_aws_authorization_cleanup_revision"),
        CheckConstraint(
            "reconcile_attempt_count >= 0",
            name="ck_aws_authorization_cleanup_attempts",
        ),
    )

    # No foreign key: the tombstone outlives the account whose authorization it is
    # still tearing down, which is the whole reason it is written separately.
    user_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    connection_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    account_id: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    next_reconcile_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconcile_attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
