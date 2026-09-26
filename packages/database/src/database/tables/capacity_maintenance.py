from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DDL,
    BigInteger,
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


class CapacityMaintenanceTable(IdTable, DatabaseBase):
    __tablename__ = "capacity_maintenance"
    __table_args__: tuple[SchemaItem, ...] = (
        Index(
            "uq_capacity_maintenance_source_generation",
            "source_machine_id",
            "release_generation",
            unique=True,
        ),
        Index(
            "uq_capacity_maintenance_source",
            "source_machine_id",
            unique=True,
            postgresql_where=text("completed_at IS NULL"),
        ),
        Index(
            "uq_capacity_maintenance_replacement",
            "replacement_machine_id",
            unique=True,
            postgresql_where=text("completed_at IS NULL AND replacement_machine_id IS NOT NULL"),
        ),
        Index(
            "ix_capacity_maintenance_active_pool",
            "pool_id",
            postgresql_where=text("completed_at IS NULL"),
        ),
        Index("ix_capacity_maintenance_pool", "pool_id"),
        CheckConstraint(
            "kind IN ('runtime', 'reserve_refresh')", name="ck_capacity_maintenance_kind"
        ),
        CheckConstraint(
            "phase IN ('planned', 'preparing', 'draining', 'verifying', "
            "'retiring', 'complete', 'failed')",
            name="ck_capacity_maintenance_phase",
        ),
        CheckConstraint(
            "release_generation > 0 AND surge_machines >= 0 AND running_cpu_millicores >= 0 "
            "AND (hourly_cost_micros IS NULL OR hourly_cost_micros >= 0)",
            name="ck_capacity_maintenance_commitments",
        ),
        CheckConstraint(
            "reserved_cpu_millicores >= 0 AND reserved_memory_mib >= 0 "
            "AND reserved_gpu_count >= 0 AND reserved_disk_bytes >= 0 "
            "AND reserved_disk_volumes >= 0",
            name="ck_capacity_maintenance_reservations",
        ),
        CheckConstraint(
            "(phase = 'complete') = (completed_at IS NOT NULL)",
            name="ck_capacity_maintenance_completion",
        ),
        CheckConstraint(
            "replacement_machine_id IS NULL OR replacement_machine_id <> source_machine_id",
            name="ck_capacity_maintenance_replacement",
        ),
    )

    pool_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("compute_units.id", ondelete="CASCADE"), nullable=False
    )
    source_machine_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    release_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    surge_machines: Mapped[int] = mapped_column(Integer, nullable=False)
    running_cpu_millicores: Mapped[int] = mapped_column(BigInteger, nullable=False)
    hourly_cost_micros: Mapped[int | None] = mapped_column(BigInteger)
    replacement_machine_id: Mapped[str | None] = mapped_column(uuid_type)
    reserved_cpu_millicores: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reserved_memory_mib: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reserved_gpu_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reserved_disk_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reserved_disk_volumes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(512), nullable=False)


event.listen(
    CapacityMaintenanceTable.__table__,
    "after_create",
    DDL("""
CREATE OR REPLACE FUNCTION enforce_capacity_maintenance_ownership()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.completed_at IS NULL THEN
            RAISE EXCEPTION 'active capacity maintenance requires cleanup'
                USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.source_machine_id IS DISTINCT FROM OLD.source_machine_id
        OR NEW.pool_id IS DISTINCT FROM OLD.pool_id
        OR NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.release_generation < OLD.release_generation
        OR (OLD.replacement_machine_id IS NOT NULL
            AND NEW.replacement_machine_id IS DISTINCT FROM OLD.replacement_machine_id
            AND (NEW.replacement_machine_id IS NOT NULL OR EXISTS (
                SELECT 1 FROM compute_provider_instances
                WHERE machine_id = OLD.replacement_machine_id
                    AND status <> 'deleted'
            )))
        OR (OLD.completed_at IS NOT NULL AND NEW IS DISTINCT FROM OLD)
    THEN
        RAISE EXCEPTION 'capacity maintenance ownership cannot be reopened'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER capacity_maintenance_ownership_fence
BEFORE UPDATE OR DELETE ON capacity_maintenance
FOR EACH ROW EXECUTE FUNCTION enforce_capacity_maintenance_ownership();
""").execute_if(dialect="postgresql"),
)
