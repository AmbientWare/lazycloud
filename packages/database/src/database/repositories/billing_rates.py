from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_rates import ComputeRateTable, PlatformRateTable
from shared.billing_quotes import BilledDimension, ContainerShape, LedgerComponent, Quote
from shared.errors import ConflictError, InvalidInputError
from shared.timestamps import to_utc, to_utc_or_none
from shared.usage import UsageBillingOwner
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ComputeRateRepository:
    """The published per-second rates for each resource a shape class holds."""

    session: Session

    def quotes_for(
        self,
        *,
        shape: ContainerShape,
        components: Sequence[LedgerComponent],
        started_at: datetime,
        ended_at: datetime,
    ) -> tuple[Quote, ...]:
        """Every quote in force at any instant of `[started_at, ended_at)`.

        One statement for every component the record prices, because a rate row
        carries all four and splitting the read would ask the same index the same
        question four times.

        A pure `timestamptz` comparison, so which rate applies never depends on
        the session `TimeZone` and is exact to the millisecond rather than to a
        day whose boundary the connection decides.
        """

        rows = self.session.scalars(
            select(ComputeRateTable)
            .where(
                ComputeRateTable.billing_owner == shape.billing_owner.value,
                ComputeRateTable.gpu_type == shape.gpu_type,
                ComputeRateTable.effective_at < ended_at,
                or_(
                    ComputeRateTable.valid_until.is_(None),
                    ComputeRateTable.valid_until > started_at,
                ),
            )
            .order_by(ComputeRateTable.effective_at)
        ).all()
        return tuple(_compute_quote(row, component) for row in rows for component in components)

    def publish(
        self,
        *,
        billing_owner: UsageBillingOwner,
        gpu_type: str,
        pricing_version: str,
        effective_at: datetime,
        nanos_per_container_second: Decimal,
        nanos_per_cpu_core_second: Decimal,
        nanos_per_memory_gib_second: Decimal,
        nanos_per_gpu_card_second: Decimal,
    ) -> None:
        """Close the rate in force and open its successor, in one transaction.

        Refuses an `effective_at` at or before the newest instant this shape
        class has a frozen segment for. Reaching further back would reprice usage
        the ledger has already shown a customer, so the two would disagree about
        what was charged and only the ledger would be right.
        """

        _require_unfrozen(
            self.session,
            effective_at,
            subject=f"{billing_owner.value}/{gpu_type or 'cpu'}",
            frozen=self.session.scalar(
                select(func.max(BillingLedgerSegmentTable.segment_ended_at)).where(
                    BillingLedgerSegmentTable.dimension == BilledDimension.ComputeRuntime.value,
                    BillingLedgerSegmentTable.billing_owner == billing_owner.value,
                    BillingLedgerSegmentTable.gpu_type == gpu_type,
                )
            ),
        )
        predecessor = self.session.scalars(
            select(ComputeRateTable)
            .where(
                ComputeRateTable.billing_owner == billing_owner.value,
                ComputeRateTable.gpu_type == gpu_type,
                ComputeRateTable.effective_at < effective_at,
                or_(
                    ComputeRateTable.valid_until.is_(None),
                    ComputeRateTable.valid_until > effective_at,
                ),
            )
            .with_for_update()
        ).first()
        if predecessor is not None:
            predecessor.valid_until = effective_at
        self.session.add(
            ComputeRateTable(
                id=str(uuid4()),
                billing_owner=billing_owner.value,
                gpu_type=gpu_type,
                pricing_version=pricing_version,
                effective_at=effective_at,
                valid_until=None,
                nanos_per_container_second=nanos_per_container_second,
                nanos_per_cpu_core_second=nanos_per_cpu_core_second,
                nanos_per_memory_gib_second=nanos_per_memory_gib_second,
                nanos_per_gpu_card_second=nanos_per_gpu_card_second,
            )
        )
        _flush(self.session, f"{billing_owner.value}/{gpu_type or 'cpu'} at {effective_at}")


@dataclass(frozen=True, slots=True)
class PlatformRateRepository:
    """The published rates that do not depend on what was placed."""

    session: Session

    def quotes_for(
        self,
        *,
        component: LedgerComponent,
        started_at: datetime,
        ended_at: datetime,
    ) -> tuple[Quote, ...]:
        rows = self.session.scalars(
            select(PlatformRateTable)
            .where(
                PlatformRateTable.effective_at < ended_at,
                or_(
                    PlatformRateTable.valid_until.is_(None),
                    PlatformRateTable.valid_until > started_at,
                ),
            )
            .order_by(PlatformRateTable.effective_at)
        ).all()
        return tuple(_platform_quote(row, component) for row in rows)

    def publish(
        self,
        *,
        pricing_version: str,
        effective_at: datetime,
        nanos_per_egress_byte: Decimal,
        nanos_per_volume_byte_second: Decimal,
    ) -> None:
        """Close the rate in force and open its successor, in one transaction.

        Refused at or before the newest instant any platform-rated dimension has
        a frozen segment for, for the same reason as compute above.

        Both rates are stated. Publishing a row that leaves one out is refused by
        the column rather than charging that dimension at zero, which is what
        keeps a deliberate zero distinguishable from an omission.
        """

        _require_unfrozen(
            self.session,
            effective_at,
            subject="platform rates",
            frozen=self.session.scalar(
                select(func.max(BillingLedgerSegmentTable.segment_ended_at)).where(
                    BillingLedgerSegmentTable.dimension != BilledDimension.ComputeRuntime.value,
                )
            ),
        )
        predecessor = self.session.scalars(
            select(PlatformRateTable)
            .where(
                PlatformRateTable.effective_at < effective_at,
                or_(
                    PlatformRateTable.valid_until.is_(None),
                    PlatformRateTable.valid_until > effective_at,
                ),
            )
            .with_for_update()
        ).first()
        if predecessor is not None:
            predecessor.valid_until = effective_at
        self.session.add(
            PlatformRateTable(
                id=str(uuid4()),
                pricing_version=pricing_version,
                effective_at=effective_at,
                valid_until=None,
                nanos_per_egress_byte=nanos_per_egress_byte,
                nanos_per_volume_byte_second=nanos_per_volume_byte_second,
            )
        )
        _flush(self.session, f"platform rates at {effective_at}")


def _compute_quote(row: ComputeRateTable, component: LedgerComponent) -> Quote:
    if component is LedgerComponent.ContainerTime:
        rate = row.nanos_per_container_second
    elif component is LedgerComponent.Cpu:
        rate = row.nanos_per_cpu_core_second
    elif component is LedgerComponent.Memory:
        rate = row.nanos_per_memory_gib_second
    elif component is LedgerComponent.Gpu:
        rate = row.nanos_per_gpu_card_second
    else:
        raise ValueError(f"{component} is not a shape-rated component")
    return Quote(
        component=component,
        rate_nanos_per_unit=rate,
        pricing_version=row.pricing_version,
        effective_at=to_utc(row.effective_at),
        valid_until=to_utc_or_none(row.valid_until),
    )


def _platform_quote(row: PlatformRateTable, component: LedgerComponent) -> Quote:
    if component is LedgerComponent.Egress:
        rate = row.nanos_per_egress_byte
    elif component is LedgerComponent.VolumeStorage:
        rate = row.nanos_per_volume_byte_second
    else:
        raise ValueError(f"{component} is not a platform-rated component")
    return Quote(
        component=component,
        rate_nanos_per_unit=rate,
        pricing_version=row.pricing_version,
        effective_at=to_utc(row.effective_at),
        valid_until=to_utc_or_none(row.valid_until),
    )


def _require_unfrozen(
    session: Session, effective_at: datetime, *, subject: str, frozen: datetime | None
) -> None:
    """Refuse a rate that would reprice usage a customer has already been shown.

    The rule is about the ledger, not the clock. Segments are append-only, so a
    rate may open anywhere no segment has been frozen yet — which is what lets
    the first rate ever published cover usage already metered, and what lets an
    outage be corrected afterwards. Requiring the future instead made both
    impossible: every instant before the first publish was permanently
    unbillable, because a rate could never reach back to it and a segment could
    never be written without one.
    """

    moment = to_utc(effective_at)
    if frozen is not None and moment <= to_utc(frozen):
        raise InvalidInputError(
            f"a published rate for {subject} cannot take effect at or before "
            f"{to_utc(frozen).isoformat()}, which the ledger has already frozen"
        )


def _flush(session: Session, subject: str) -> None:
    try:
        session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"a published rate already covers {subject}") from exc


__all__ = ["ComputeRateRepository", "PlatformRateRepository"]
