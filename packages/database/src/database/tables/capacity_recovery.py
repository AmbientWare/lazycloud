from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdTable, uuid_type


class CapacityRecoveryTable(IdTable, DatabaseBase):
    __tablename__ = "capacity_recoveries"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("uq_capacity_recoveries_source", "source_machine_id", unique=True),
        Index(
            "ix_capacity_recoveries_due",
            "next_action_at",
            "id",
            postgresql_where=text("completed_at IS NULL"),
        ),
        Index("ix_capacity_recoveries_source_unit", "source_unit_id"),
        Index("ix_capacity_recoveries_target_unit", "target_unit_id"),
        Index(
            "uq_capacity_recoveries_replacement",
            "replacement_machine_id",
            unique=True,
            postgresql_where=text("replacement_machine_id IS NOT NULL"),
        ),
        CheckConstraint("attempt >= 0", name="ck_capacity_recoveries_attempt"),
        CheckConstraint(
            "(target_unit_id IS NULL) = (operation_id IS NULL)",
            name="ck_capacity_recoveries_operation",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    source_unit_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("compute_units.id", ondelete="RESTRICT"), nullable=False
    )
    source_machine_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_adjusted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    target_unit_id: Mapped[str | None] = mapped_column(
        uuid_type, ForeignKey("compute_units.id", ondelete="RESTRICT")
    )
    operation_id: Mapped[str | None] = mapped_column(uuid_type)
    replacement_machine_id: Mapped[str | None] = mapped_column(uuid_type)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_action_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")


event.listen(
    CapacityRecoveryTable.__table__,
    "after_create",
    DDL("""
CREATE OR REPLACE FUNCTION enforce_capacity_recovery_ownership()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_machine_id IS DISTINCT FROM OLD.source_machine_id
        OR NEW.source_unit_id IS DISTINCT FROM OLD.source_unit_id
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR (OLD.source_adjusted AND NOT NEW.source_adjusted)
        OR (OLD.source_applied AND NOT NEW.source_applied)
        OR NEW.attempt < OLD.attempt
        OR (OLD.deadline IS NOT NULL AND (NEW.deadline IS NULL OR NEW.deadline > OLD.deadline))
        OR (OLD.completed_at IS NOT NULL AND NEW IS DISTINCT FROM OLD)
    THEN
        RAISE EXCEPTION 'capacity recovery ownership cannot be reopened'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER capacity_recovery_ownership_fence
BEFORE UPDATE ON capacity_recoveries
FOR EACH ROW EXECUTE FUNCTION enforce_capacity_recovery_ownership();
""").execute_if(dialect="postgresql"),
)
