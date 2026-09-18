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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.schema import SchemaItem

from database.json_documents import JsonDocument
from database.tables.base import DatabaseBase, IdTable, uuid_type


def _authorization_constraints(prefix: str) -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint(
            "authorization_generation > 0 AND authorization_validation_generation >= 0",
            name=f"ck_{prefix}_generation",
        ),
        CheckConstraint(
            "authorization_mode IN ('managed_stack', 'existing_role') "
            "AND ((authorization_mode = 'managed_stack' AND managed_stack_name IS NOT NULL "
            "AND managed_region IS NOT NULL AND managed_template_version IS NOT NULL "
            "AND managed_template_sha256 IS NOT NULL AND managed_shared_ami_ids IS NOT NULL) "
            "OR (authorization_mode = 'existing_role' AND managed_stack_name IS NULL "
            "AND managed_region IS NULL AND managed_stack_id IS NULL "
            "AND managed_template_version IS NULL AND managed_template_sha256 IS NULL "
            "AND managed_shared_ami_ids IS NULL AND authorization_stack IS NULL))",
            name=f"ck_{prefix}_managed_identity",
        ),
        CheckConstraint(
            "(authorization_error_code IS NULL) = (authorization_error_message = '')",
            name=f"ck_{prefix}_error",
        ),
        CheckConstraint(
            "authorization_phase <> 'ready' OR authorization_last_validated_at IS NOT NULL",
            name=f"ck_{prefix}_validation",
        ),
    )


class AuthorizationColumns:
    authorization_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    authorization_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authorization_role_arn: Mapped[str] = mapped_column(Text, nullable=False)
    authorization_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    authorization_phase: Mapped[str] = mapped_column(Text, nullable=False)
    authorization_validation_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authorization_last_validation_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    authorization_last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    authorization_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    authorization_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorization_error_message: Mapped[str] = mapped_column(Text, nullable=False)
    authorization_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    authorization_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    managed_stack_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    managed_region: Mapped[str | None] = mapped_column(Text, nullable=True)
    managed_stack_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    managed_template_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    managed_template_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    managed_shared_ami_ids: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(255)), nullable=True
    )
    authorization_stack: Mapped[dict[str, JsonValue] | None] = mapped_column(
        JsonDocument(none_as_null=True), nullable=True
    )


class AwsAccountConnectionTable(IdTable, DatabaseBase):
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
            "(claim_token IS NULL) = (claim_expires_at IS NULL)",
            name="ck_aws_account_connections_claim",
        ),
        CheckConstraint(
            "(node_role_arn IS NULL) = (node_instance_profile_arn IS NULL)",
            name="ck_aws_account_connections_node_identity",
        ),
        CheckConstraint(
            "drain_remaining_units >= 0 AND drain_total_units >= drain_remaining_units",
            name="ck_aws_account_connections_drain",
        ),
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
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    next_reconcile_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_token: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconcile_attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    provider_operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_operation_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    node_role_arn: Mapped[str | None] = mapped_column(Text, nullable=True)
    node_instance_profile_arn: Mapped[str | None] = mapped_column(Text, nullable=True)
    drain_total_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    drain_remaining_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    customer_action_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_action_label: Mapped[str] = mapped_column(Text, nullable=False)
    bucket_access_reconcile_pending: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, nullable=False)
    authorizations: Mapped[list[AwsAuthorizationGenerationTable]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", passive_deletes=True
    )
    network_rows: Mapped[list[AwsAccountNetworkTable]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", passive_deletes=True
    )


class AwsAuthorizationCleanupTombstoneTable(AuthorizationColumns, IdTable, DatabaseBase):
    __tablename__ = "aws_authorization_cleanup_tombstones"
    __table_args__: tuple[SchemaItem, ...] = (
        *_authorization_constraints("aws_cleanup_authorization"),
        CheckConstraint(
            "(claim_token IS NULL) = (claim_expires_at IS NULL)",
            name="ck_aws_authorization_cleanup_claim",
        ),
        CheckConstraint(
            "(node_role_arn IS NULL) = (node_instance_profile_arn IS NULL) "
            "AND (NOT remove_node_identity OR node_role_arn IS NOT NULL)",
            name="ck_aws_authorization_cleanup_node_identity",
        ),
        CheckConstraint("expires_at > created_at", name="ck_aws_authorization_cleanup_expiry"),
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
    claim_token: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconcile_attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_role_arn: Mapped[str | None] = mapped_column(Text, nullable=True)
    node_instance_profile_arn: Mapped[str | None] = mapped_column(Text, nullable=True)
    remove_node_identity: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, nullable=False)


class AwsAuthorizationGenerationTable(AuthorizationColumns, DatabaseBase):
    __tablename__ = "aws_authorization_generations"
    __table_args__ = (
        *_authorization_constraints("aws_authorization"),
        CheckConstraint(
            "slot IN ('active', 'pending', 'retiring')", name="ck_aws_authorization_slot"
        ),
        UniqueConstraint(
            "connection_id",
            "authorization_generation",
            name="uq_aws_authorization_generation",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "connection_id",
            "authorization_id",
            name="uq_aws_authorization_identity",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    connection_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("aws_account_connections.id", ondelete="CASCADE"), primary_key=True
    )
    slot: Mapped[str] = mapped_column(String(16), primary_key=True)


class AwsAccountNetworkTable(DatabaseBase):
    __tablename__ = "aws_account_networks"
    __table_args__ = (
        CheckConstraint("cardinality(subnet_ids) >= 2", name="ck_aws_account_network_subnets"),
    )
    connection_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("aws_account_connections.id", ondelete="CASCADE"), primary_key=True
    )
    region: Mapped[str] = mapped_column(String(64), primary_key=True)
    vpc_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subnet_ids: Mapped[list[str]] = mapped_column(ARRAY(String(128)), nullable=False)
    security_group_id: Mapped[str] = mapped_column(String(128), nullable=False)
