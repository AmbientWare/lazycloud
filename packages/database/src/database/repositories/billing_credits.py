from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from uuid import uuid4

from database.repositories.billing import BillingAccountRepository
from database.repositories.storage_retention import StorageRetentionRepository
from database.tables.billing_credit_adjustments import BillingCreditAdjustmentTable
from database.tables.billing_credits import (
    BillingCreditAllocationTable,
    BillingCreditCutoverTable,
    BillingCreditLotTable,
    BillingCreditSettlementTable,
)
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_outbox import BillingMeterOutboxTable
from shared.billing_credits import (
    CreditGrant,
    CreditKind,
    CreditSettlement,
)
from shared.billing_quotes import BilledDimension
from shared.errors import ConflictError, NotFoundError
from shared.timestamps import to_utc, utc_now
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class CreditCutover:
    effective_at: datetime
    completed_at: datetime | None
    blocked_reason: str


@dataclass(frozen=True, slots=True)
class CreditAdjustments:
    credited_nanos: int = 0
    unsettled_nanos: int = 0
    waived_nanos: int = 0
    unpaid_nanos: int = 0


@dataclass(frozen=True, slots=True)
class BillingCreditRepository:
    session: Session

    def cutover(self, *, user_id: str) -> CreditCutover | None:
        row = self.session.get(BillingCreditCutoverTable, user_id)
        if row is None:
            return None
        return CreditCutover(
            effective_at=to_utc(row.effective_at),
            completed_at=to_utc(row.completed_at) if row.completed_at else None,
            blocked_reason=row.blocked_reason,
        )

    def prepare_cutover(self, *, user_id: str, effective_at: datetime) -> CreditCutover:
        self._lock(user_id)
        existing = self.cutover(user_id=user_id)
        if existing is not None:
            return existing
        boundary = to_utc(effective_at)
        exported = self.session.scalar(
            select(BillingLedgerSegmentTable.id)
            .where(
                BillingLedgerSegmentTable.owner_user_id == user_id,
                BillingLedgerSegmentTable.segment_ended_at > boundary,
            )
            .limit(1)
        )
        if exported is not None:
            raise ConflictError(
                "gross usage already exists at or after the proposed credit cutover"
            )
        self.session.add(
            BillingCreditCutoverTable(
                user_id=user_id, effective_at=boundary, completed_at=None, blocked_reason=""
            )
        )
        self.session.flush()
        return CreditCutover(boundary, None, "")

    def complete_cutover(self, *, user_id: str, at: datetime) -> None:
        self._lock(user_id)
        row = self.session.get(BillingCreditCutoverTable, user_id)
        if row is None:
            raise NotFoundError("the billing account has no prepared credit cutover")
        if row.completed_at is not None:
            return
        moment = to_utc(at)
        if moment < to_utc(row.effective_at):
            raise ConflictError("credit cutover cannot complete before its settlement boundary")
        row.completed_at = moment
        row.blocked_reason = ""
        self.session.flush()

    def block_cutover(self, *, user_id: str, reason: str) -> None:
        self._lock(user_id)
        row = self.session.get(BillingCreditCutoverTable, user_id)
        if row is None:
            raise NotFoundError("the billing account has no prepared credit cutover")
        if row.completed_at is None:
            row.blocked_reason = reason[:1024]
            self.session.flush()

    def issue(self, *, user_id: str, grant: CreditGrant) -> str:
        self._lock(user_id)
        existing = self.session.scalar(
            select(BillingCreditLotTable).where(
                BillingCreditLotTable.user_id == user_id,
                BillingCreditLotTable.source_id == grant.source_id,
            )
        )
        if existing is not None:
            held = CreditGrant(
                source_id=existing.source_id,
                kind=CreditKind(existing.kind),
                amount_nanos=existing.amount_nanos,
                effective_at=to_utc(existing.effective_at),
                expires_at=to_utc(existing.expires_at) if existing.expires_at else None,
            )
            if held != grant:
                raise ConflictError("a credit source cannot be reused with different terms")
            self._pay_debt(user_id=user_id, at=utc_now())
            return existing.id
        row = BillingCreditLotTable(
            id=str(uuid4()),
            user_id=user_id,
            source_id=grant.source_id,
            kind=grant.kind.value,
            amount_nanos=grant.amount_nanos,
            effective_at=to_utc(grant.effective_at),
            expires_at=to_utc(grant.expires_at) if grant.expires_at else None,
        )
        self.session.add(row)
        self.session.flush()
        self._pay_debt(user_id=user_id, at=utc_now())
        return row.id

    def balance(self, *, user_id: str, at: datetime) -> int:
        self._lock(user_id)
        self._pay_debt(user_id=user_id, at=at)
        return sum(amount for _, amount in self._lot_balances(user_id=user_id, at=at)) - sum(
            row.payable_nanos or 0 for row in self._outstanding(user_id=user_id)
        )

    def adjust(
        self,
        *,
        user_id: str,
        credit_lot_id: str,
        source_id: str,
        amount_nanos: int,
        effective_at: datetime,
    ) -> str:
        self._lock(user_id)
        if not source_id or len(source_id) > 255 or amount_nanos == 0:
            raise ConflictError("a credit adjustment needs a stable source and a nonzero amount")
        lot = self.session.get(BillingCreditLotTable, credit_lot_id)
        if lot is None or lot.user_id != user_id:
            raise NotFoundError("the credit lot does not belong to this billing account")
        moment = to_utc(effective_at)
        existing = self.session.scalar(
            select(BillingCreditAdjustmentTable).where(
                BillingCreditAdjustmentTable.credit_lot_id == credit_lot_id,
                BillingCreditAdjustmentTable.source_id == source_id,
            )
        )
        if existing is not None:
            if existing.amount_nanos != amount_nanos or to_utc(existing.effective_at) != moment:
                raise ConflictError(
                    "a credit adjustment source cannot be reused with different terms"
                )
            return existing.id
        row = BillingCreditAdjustmentTable(
            id=str(uuid4()),
            credit_lot_id=credit_lot_id,
            source_id=source_id,
            amount_nanos=amount_nanos,
            effective_at=moment,
        )
        self.session.add(row)
        self.session.flush()
        self._pay_debt(user_id=user_id, at=utc_now())
        return row.id

    def subscription_issued(self, *, user_id: str, period_ended_at: datetime) -> int:
        return int(
            self.session.scalar(
                select(func.coalesce(func.sum(BillingCreditLotTable.amount_nanos), 0)).where(
                    BillingCreditLotTable.user_id == user_id,
                    BillingCreditLotTable.kind == CreditKind.Subscription.value,
                    BillingCreditLotTable.expires_at == period_ended_at,
                )
            )
            or 0
        )

    def subscription_sources(self, *, user_id: str, period_ended_at: datetime) -> frozenset[str]:
        return frozenset(
            self.session.scalars(
                select(BillingCreditLotTable.source_id).where(
                    BillingCreditLotTable.user_id == user_id,
                    BillingCreditLotTable.kind == CreditKind.Subscription.value,
                    BillingCreditLotTable.expires_at == period_ended_at,
                )
            )
        )

    def settle(
        self, *, user_id: str, usage_record_id: str, waived: bool
    ) -> CreditSettlement | None:
        self._lock(user_id)
        existing = self.session.get(BillingCreditSettlementTable, usage_record_id)
        if existing is not None:
            if existing.user_id != user_id:
                raise ConflictError("usage settlement belongs to a different billing account")
            if existing.settled_at is not None:
                return _settlement(existing)
        cutover = self.cutover(user_id=user_id)
        if cutover is None:
            raise ConflictError("usage has no local credit settlement boundary")
        segments = self.session.scalars(
            select(BillingLedgerSegmentTable)
            .where(
                BillingLedgerSegmentTable.usage_record_id == usage_record_id,
                BillingLedgerSegmentTable.segment_started_at >= cutover.effective_at,
            )
            .order_by(BillingLedgerSegmentTable.segment_started_at, BillingLedgerSegmentTable.id)
        ).all()
        if not segments or any(segment.owner_user_id != user_id for segment in segments):
            raise NotFoundError("no priced usage belongs to this billing account")
        if (
            self.session.scalar(
                select(BillingMeterOutboxTable.id).where(
                    BillingMeterOutboxTable.usage_record_id == usage_record_id,
                    BillingMeterOutboxTable.occurred_at >= cutover.effective_at,
                )
            )
            is not None
        ):
            raise ConflictError("usage already queued for billing cannot be repriced with credits")
        if existing is None:
            existing = BillingCreditSettlementTable(
                usage_record_id=usage_record_id,
                user_id=user_id,
                gross_nanos=sum(segment.cost_nanos for segment in segments),
            )
            self.session.add(existing)
            self.session.flush()
        if not waived and cutover.completed_at is None:
            return None
        credited = 0
        waived_cost = existing.gross_nanos if waived else self.retained_storage_cost(list(segments))
        for segment in () if waived else segments:
            for started_at, ended_at, remaining_cost, retained in self._cost_slices(segment):
                if retained:
                    continue
                for lot, available in self._available(user_id=user_id, at=started_at):
                    amount = min(remaining_cost, available)
                    if amount <= 0:
                        break
                    self._allocate(lot.id, segment.id, started_at, ended_at, amount)
                    credited += amount
                    remaining_cost -= amount
                self.session.flush()
        existing.credited_nanos = credited
        existing.waived_nanos = waived_cost
        existing.payable_nanos = existing.gross_nanos - credited - waived_cost
        existing.settled_at = utc_now()
        self.session.flush()
        self._pay_debt(user_id=user_id, at=utc_now())
        return _settlement(existing)

    def retained_storage_cost(self, segments: list[BillingLedgerSegmentTable]) -> int:
        return sum(
            cost
            for segment in segments
            for _, _, cost, retained in self._cost_slices(segment)
            if retained
        )

    def _cost_slices(
        self, segment: BillingLedgerSegmentTable
    ) -> list[tuple[datetime, datetime, int, bool]]:
        start = to_utc(segment.segment_started_at)
        end = to_utc(segment.segment_ended_at)
        boundaries = {start, end}
        retention = (
            StorageRetentionRepository(self.session).intervals(
                user_id=segment.owner_user_id, started_at=start, ended_at=end
            )
            if segment.dimension == BilledDimension.VolumeStorage.value
            else ()
        )
        for lower, upper in retention:
            boundaries.update((lower, upper))
        lots = self.session.scalars(
            select(BillingCreditLotTable).where(
                BillingCreditLotTable.user_id == segment.owner_user_id,
                BillingCreditLotTable.effective_at < end,
                or_(
                    BillingCreditLotTable.expires_at.is_(None),
                    BillingCreditLotTable.expires_at > start,
                ),
            )
        ).all()
        for lot in lots:
            for instant in (lot.effective_at, lot.expires_at):
                if instant is not None and start < to_utc(instant) < end:
                    boundaries.add(to_utc(instant))
        ordered = sorted(boundaries)
        duration = (end - start) // timedelta(microseconds=1)
        # Cumulative integer shares preserve the frozen total across every split.
        return [
            (
                lower,
                upper,
                segment.cost_nanos * ((upper - start) // timedelta(microseconds=1)) // duration
                - segment.cost_nanos * ((lower - start) // timedelta(microseconds=1)) // duration,
                any(
                    retained_start <= lower and upper <= retained_end
                    for retained_start, retained_end in retention
                ),
            )
            for lower, upper in pairwise(ordered)
        ]

    def pending_records(self, *, user_id: str) -> tuple[str, ...]:
        return tuple(
            self.session.scalars(
                select(BillingCreditSettlementTable.usage_record_id)
                .where(
                    BillingCreditSettlementTable.user_id == user_id,
                    BillingCreditSettlementTable.settled_at.is_(None),
                )
                .order_by(
                    BillingCreditSettlementTable.created_at,
                    BillingCreditSettlementTable.usage_record_id,
                )
            ).all()
        )

    def account_adjustments(
        self, *, user_id: str, start: datetime, end: datetime
    ) -> Mapping[BilledDimension, CreditAdjustments]:
        window = (
            BillingLedgerSegmentTable.owner_user_id == user_id,
            BillingLedgerSegmentTable.segment_started_at >= start,
            BillingLedgerSegmentTable.segment_started_at < end,
        )
        allocated = {
            row[0]: row[1]
            for row in self.session.execute(
                select(
                    BillingLedgerSegmentTable.dimension,
                    func.sum(BillingCreditAllocationTable.amount_nanos),
                )
                .join(
                    BillingCreditAllocationTable,
                    BillingCreditAllocationTable.ledger_segment_id == BillingLedgerSegmentTable.id,
                )
                .where(*window)
                .group_by(BillingLedgerSegmentTable.dimension)
            ).all()
        }
        pending = {
            row[0]: row[1]
            for row in self.session.execute(
                select(
                    BillingLedgerSegmentTable.dimension,
                    func.sum(BillingLedgerSegmentTable.cost_nanos),
                )
                .join(
                    BillingCreditSettlementTable,
                    BillingCreditSettlementTable.usage_record_id
                    == BillingLedgerSegmentTable.usage_record_id,
                )
                .join(
                    BillingCreditCutoverTable,
                    BillingCreditCutoverTable.user_id == BillingCreditSettlementTable.user_id,
                )
                .where(
                    *window,
                    BillingCreditSettlementTable.settled_at.is_(None),
                    BillingLedgerSegmentTable.segment_started_at
                    >= BillingCreditCutoverTable.effective_at,
                )
                .group_by(BillingLedgerSegmentTable.dimension)
            ).all()
        }
        outstanding = {row.usage_record_id for row in self._outstanding(user_id=user_id)}
        spent = (
            select(
                BillingCreditAllocationTable.ledger_segment_id,
                func.sum(BillingCreditAllocationTable.amount_nanos).label("amount"),
            )
            .group_by(BillingCreditAllocationTable.ledger_segment_id)
            .subquery()
        )
        waived: dict[str, int] = {}
        unpaid: dict[str, int] = {}
        for segment, settlement, allocated_amount in self.session.execute(
            select(
                BillingLedgerSegmentTable,
                BillingCreditSettlementTable,
                func.coalesce(spent.c.amount, 0),
            )
            .join(
                BillingCreditSettlementTable,
                BillingCreditSettlementTable.usage_record_id
                == BillingLedgerSegmentTable.usage_record_id,
            )
            .join(
                BillingCreditCutoverTable,
                BillingCreditCutoverTable.user_id == BillingLedgerSegmentTable.owner_user_id,
            )
            .outerjoin(spent, spent.c.ledger_segment_id == BillingLedgerSegmentTable.id)
            .where(
                *window,
                BillingLedgerSegmentTable.segment_started_at
                >= BillingCreditCutoverTable.effective_at,
            )
        ).all():
            waived_cost = (
                segment.cost_nanos
                if settlement.waived_nanos == settlement.gross_nanos
                else self.retained_storage_cost([segment])
                if settlement.waived_nanos
                else 0
            )
            waived[segment.dimension] = waived.get(segment.dimension, 0) + waived_cost
            if segment.usage_record_id in outstanding:
                unpaid[segment.dimension] = unpaid.get(segment.dimension, 0) + max(
                    0, segment.cost_nanos - allocated_amount - waived_cost
                )
        return {
            dimension: CreditAdjustments(
                credited_nanos=int(allocated.get(dimension.value, 0)),
                unsettled_nanos=int(pending.get(dimension.value, 0)),
                waived_nanos=int(waived.get(dimension.value, 0)),
                unpaid_nanos=unpaid.get(dimension.value, 0),
            )
            for dimension in BilledDimension
        }

    def _allocate(
        self, lot_id: str, segment_id: str, start: datetime, end: datetime, amount: int
    ) -> None:
        self.session.add(
            BillingCreditAllocationTable(
                id=str(uuid4()),
                credit_lot_id=lot_id,
                ledger_segment_id=segment_id,
                started_at=start,
                ended_at=end,
                amount_nanos=amount,
            )
        )
        self.session.flush()

    def _outstanding(self, *, user_id: str) -> list[BillingCreditSettlementTable]:
        return list(
            self.session.scalars(
                select(BillingCreditSettlementTable)
                .join(
                    BillingCreditCutoverTable,
                    BillingCreditCutoverTable.user_id == BillingCreditSettlementTable.user_id,
                )
                .where(
                    BillingCreditSettlementTable.user_id == user_id,
                    BillingCreditSettlementTable.settled_at.is_not(None),
                    BillingCreditSettlementTable.payable_nanos > 0,
                    ~select(BillingMeterOutboxTable.id)
                    .where(
                        BillingMeterOutboxTable.usage_record_id
                        == BillingCreditSettlementTable.usage_record_id,
                        BillingMeterOutboxTable.occurred_at
                        >= BillingCreditCutoverTable.effective_at,
                    )
                    .exists(),
                )
                .order_by(
                    BillingCreditSettlementTable.created_at,
                    BillingCreditSettlementTable.usage_record_id,
                )
            )
        )

    def _pay_debt(self, *, user_id: str, at: datetime) -> None:
        balances = self._lot_balances(user_id=user_id, at=at)
        available = [(lot, amount) for lot, amount in balances if amount > 0]
        for debtor, remaining in balances:
            debt = max(0, -remaining)
            for index, (lot, amount) in enumerate(available):
                paid = min(debt, amount)
                if paid == 0:
                    continue
                source = f"wallet-offset:{uuid4()}"
                for target, adjustment in ((lot, -paid), (debtor, paid)):
                    self.session.add(
                        BillingCreditAdjustmentTable(
                            id=str(uuid4()),
                            credit_lot_id=target.id,
                            source_id=source,
                            amount_nanos=adjustment,
                            effective_at=to_utc(at),
                        )
                    )
                debt -= paid
                available[index] = (lot, amount - paid)
        self.session.flush()
        for settlement in self._outstanding(user_id=user_id):
            remaining = settlement.payable_nanos or 0
            segments = self.session.scalars(
                select(BillingLedgerSegmentTable)
                .join(
                    BillingCreditCutoverTable,
                    BillingCreditCutoverTable.user_id == BillingLedgerSegmentTable.owner_user_id,
                )
                .where(
                    BillingLedgerSegmentTable.usage_record_id == settlement.usage_record_id,
                    BillingLedgerSegmentTable.segment_started_at
                    >= BillingCreditCutoverTable.effective_at,
                )
                .order_by(
                    BillingLedgerSegmentTable.segment_started_at, BillingLedgerSegmentTable.id
                )
            ).all()
            for segment in segments:
                allocations = self.session.scalars(
                    select(BillingCreditAllocationTable).where(
                        BillingCreditAllocationTable.ledger_segment_id == segment.id
                    )
                ).all()
                for start, end, cost, retained in self._cost_slices(segment):
                    if retained:
                        continue
                    owed = min(
                        remaining,
                        cost - sum(_allocation_share(row, start, end) for row in allocations),
                    )
                    # Delayed renewal evidence can arrive after expiry. That
                    # credit still covers unpaid usage within its original term.
                    expired = [
                        (lot, amount)
                        for lot, amount in self._available(user_id=user_id, at=start)
                        if lot.expires_at is not None and to_utc(lot.expires_at) <= to_utc(at)
                    ]
                    for lot, amount in expired:
                        paid = min(owed, amount)
                        if paid <= 0:
                            continue
                        self._allocate(lot.id, segment.id, start, end, paid)
                        owed -= paid
                        remaining -= paid
                    for index, (lot, amount) in enumerate(available):
                        paid = min(owed, amount)
                        if paid <= 0:
                            continue
                        self._allocate(lot.id, segment.id, start, end, paid)
                        owed -= paid
                        remaining -= paid
                        available[index] = (lot, amount - paid)
            paid = (settlement.payable_nanos or 0) - remaining
            settlement.credited_nanos = (settlement.credited_nanos or 0) + paid
            settlement.payable_nanos = remaining
            self.session.flush()

    def _available(self, *, user_id: str, at: datetime) -> list[tuple[BillingCreditLotTable, int]]:
        return [
            (lot, amount)
            for lot, amount in self._lot_balances(user_id=user_id, at=at)
            if amount > 0
        ]

    def _lot_balances(
        self, *, user_id: str, at: datetime
    ) -> list[tuple[BillingCreditLotTable, int]]:
        spent = (
            select(
                BillingCreditAllocationTable.credit_lot_id,
                func.sum(BillingCreditAllocationTable.amount_nanos).label("amount"),
            )
            .join(
                BillingCreditLotTable,
                BillingCreditLotTable.id == BillingCreditAllocationTable.credit_lot_id,
            )
            .where(BillingCreditLotTable.user_id == user_id)
            .group_by(BillingCreditAllocationTable.credit_lot_id)
            .subquery()
        )
        moment = to_utc(at)
        adjustments = (
            select(
                BillingCreditAdjustmentTable.credit_lot_id,
                func.sum(BillingCreditAdjustmentTable.amount_nanos).label("amount"),
            )
            .join(
                BillingCreditLotTable,
                BillingCreditLotTable.id == BillingCreditAdjustmentTable.credit_lot_id,
            )
            .where(
                BillingCreditLotTable.user_id == user_id,
                BillingCreditAdjustmentTable.effective_at <= max(moment, utc_now()),
            )
            .group_by(BillingCreditAdjustmentTable.credit_lot_id)
            .subquery()
        )
        rows = self.session.execute(
            select(
                BillingCreditLotTable,
                BillingCreditLotTable.amount_nanos
                + func.coalesce(adjustments.c.amount, 0)
                - func.coalesce(spent.c.amount, 0),
            )
            .outerjoin(spent, spent.c.credit_lot_id == BillingCreditLotTable.id)
            .outerjoin(adjustments, adjustments.c.credit_lot_id == BillingCreditLotTable.id)
            .where(
                BillingCreditLotTable.user_id == user_id,
                BillingCreditLotTable.effective_at <= moment,
            )
            .order_by(
                BillingCreditLotTable.expires_at.asc().nulls_last(),
                BillingCreditLotTable.effective_at,
                BillingCreditLotTable.id,
            )
        ).all()
        return [
            (lot, int(remaining))
            for lot, remaining in rows
            if remaining < 0 or lot.expires_at is None or to_utc(lot.expires_at) > moment
        ]

    def _lock(self, user_id: str) -> None:
        if BillingAccountRepository(self.session).get_by_user(user_id, for_update=True) is None:
            raise NotFoundError("no billing account exists for these credits")


def _settlement(row: BillingCreditSettlementTable) -> CreditSettlement:
    if row.credited_nanos is None or row.payable_nanos is None:
        raise ConflictError("credit settlement has not completed")
    return CreditSettlement(row.gross_nanos, row.credited_nanos, row.payable_nanos)


def _allocation_share(row: BillingCreditAllocationTable, start: datetime, end: datetime) -> int:
    lower = max(start, to_utc(row.started_at))
    upper = min(end, to_utc(row.ended_at))
    if lower >= upper:
        return 0
    origin = to_utc(row.started_at)
    duration = (to_utc(row.ended_at) - origin) // timedelta(microseconds=1)
    return (
        row.amount_nanos * ((upper - origin) // timedelta(microseconds=1)) // duration
        - row.amount_nanos * ((lower - origin) // timedelta(microseconds=1)) // duration
    )


__all__ = ["BillingCreditRepository", "CreditAdjustments", "CreditCutover"]
