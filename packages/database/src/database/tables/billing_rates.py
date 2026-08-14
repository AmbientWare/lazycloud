from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    literal_column,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdTable

_VALIDITY_WINDOW = literal_column("tstzrange(effective_at, valid_until, '[)')", TSTZRANGE)
"""The half-open interval a rate is in force over, as PostgreSQL reads it.

Spelled out rather than built from `func` so the bound literal reaches the DDL
verbatim; a bind parameter cannot appear in a constraint definition.
"""

# Overlapping windows would make "the rate in force at t" ambiguous, and the
# answer would be whichever row came back first. SQLite cannot express a range
# exclusion, so there it holds only the unique start; PostgreSQL is production.
_COMPUTE_RATE_WINDOW = ExcludeConstraint(
    ("billing_owner", "="),
    ("gpu_type", "="),
    (_VALIDITY_WINDOW, "&&"),
    name="ex_billing_compute_rates_window",
    using="gist",
).ddl_if(dialect="postgresql")

_PLATFORM_RATE_WINDOW = ExcludeConstraint(
    (_VALIDITY_WINDOW, "&&"),
    name="ex_billing_platform_rates_window",
    using="gist",
).ddl_if(dialect="postgresql")


class ComputeRateTable(IdTable, DatabaseBase):
    """What a second of each resource a container holds costs, over one interval.

    Rows are published, never edited: a row whose `effective_at` has passed is
    what elapsed usage was billed at, and the ledger has already frozen it. A
    price change is a new row with a future `effective_at` and the predecessor's
    `valid_until` closed to the same instant.

    No rate column carries a default. An insert that omits one is refused by the
    database rather than charging that dimension at zero.
    """

    __tablename__ = "billing_compute_rates"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint(
            "billing_owner",
            "gpu_type",
            "effective_at",
            name="uq_billing_compute_rates_start",
        ),
        CheckConstraint(
            "billing_owner IN ('platform_fleet', 'connected_cloud', 'self_hosted')",
            name="ck_billing_compute_rates_owner",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > effective_at",
            name="ck_billing_compute_rates_window",
        ),
        CheckConstraint(
            "nanos_per_container_second >= 0 AND nanos_per_cpu_core_second >= 0 "
            "AND nanos_per_memory_gib_second >= 0 AND nanos_per_gpu_card_second >= 0",
            name="ck_billing_compute_rates_nonnegative",
        ),
        _COMPUTE_RATE_WINDOW,
        Index(
            "ix_billing_compute_rates_lookup",
            "billing_owner",
            "gpu_type",
            "effective_at",
        ),
    )

    billing_owner: Mapped[str] = mapped_column(String(40), nullable=False)
    gpu_type: Mapped[str] = mapped_column(String(64), nullable=False)
    """Normalized GPU model; empty is CPU-only work and is a real shape class,
    not an unset field."""
    pricing_version: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    nanos_per_container_second: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    nanos_per_cpu_core_second: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    nanos_per_memory_gib_second: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    nanos_per_gpu_card_second: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)


class PlatformRateTable(IdTable, DatabaseBase):
    """Rates that belong to the platform rather than to a container's shape.

    An egress or volume-storage rate of zero is stated in a row with a
    `pricing_version` and an `effective_at`, which is what makes turning either
    on later one insert rather than a change of code.
    """

    __tablename__ = "billing_platform_rates"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("effective_at", name="uq_billing_platform_rates_start"),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > effective_at",
            name="ck_billing_platform_rates_window",
        ),
        CheckConstraint(
            "nanos_per_egress_byte >= 0 AND nanos_per_volume_byte_second >= 0",
            name="ck_billing_platform_rates_nonnegative",
        ),
        _PLATFORM_RATE_WINDOW,
        Index("ix_billing_platform_rates_lookup", "effective_at"),
    )

    pricing_version: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    nanos_per_egress_byte: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    nanos_per_volume_byte_second: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)


__all__ = ["ComputeRateTable", "PlatformRateTable"]
