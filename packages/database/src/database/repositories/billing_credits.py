from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from uuid import uuid4

from database.repositories.billing import BillingAccountRepository
from database.tables.billing_credit_adjustments import BillingCreditAdjustmentTable
from database.tables.billing_credits import (
    BillingCreditAllocationTable,
    BillingCreditCutoverTable,
    BillingCreditLotTable,
    BillingCreditSettlementTable,
)
from database.tables.billing_funding import BillingFundingAllocationTable
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_outbox import BillingMeterOutboxTable
from shared.billing_credits import (
    CreditBalance,
    CreditGrant,
    CreditKind,
    CreditScope,
    CreditSettlement,
)
from shared.billing_quotes import BilledDimension
from shared.errors import ConflictError, NotFoundError
from shared.timestamps import to_utc, utc_now
from sqlalchemy import func, or_, select, true
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


@dataclass(frozen=True, slots=True)
class SpendableCreditLot:
    id: str
    amount_nanos: int
    expires_at: datetime | None


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
                scope=CreditScope(existing.scope),
                amount_nanos=existing.amount_nanos,
                effective_at=to_utc(existing.effective_at),
                expires_at=to_utc(existing.expires_at) if existing.expires_at else None,
            )
            if held != grant:
                raise ConflictError("a credit source cannot be reused with different terms")
            return existing.id
        row = BillingCreditLotTable(
            id=str(uuid4()),
            user_id=user_id,
            source_id=grant.source_id,
            kind=grant.kind.value,
            scope=grant.scope.value,
            amount_nanos=grant.amount_nanos,
            effective_at=to_utc(grant.effective_at),
            expires_at=to_utc(grant.expires_at) if grant.expires_at else None,
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def balance(self, *, user_id: str, at: datetime, dimension: BilledDimension) -> CreditBalance:
        totals = {kind: 0 for kind in CreditKind}
        for lot, remaining in self._lot_balances(user_id=user_id, at=at, dimension=dimension):
            totals[CreditKind(lot.kind)] += remaining
        return CreditBalance(
            purchased_nanos=totals[CreditKind.Purchased],
            subscription_nanos=totals[CreditKind.Subscription],
            trial_nanos=totals[CreditKind.Trial],
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
        return row.id

    def debt_nanos(self, *, user_id: str, at: datetime) -> int:
        return max(
            0,
            -self.balance(
                user_id=user_id,
                at=at,
                dimension=BilledDimension.ComputeRuntime,
            ).purchased_nanos,
        )

    def spendable_lots(
        self,
        *,
        user_id: str,
        at: datetime,
        dimension: BilledDimension,
        container_id: str = "",
    ) -> tuple[SpendableCreditLot, ...]:
        return tuple(
            SpendableCreditLot(lot.id, amount, to_utc(lot.expires_at) if lot.expires_at else None)
            for lot, amount in self._available(
                user_id=user_id,
                at=at,
                dimension=dimension,
                container_id=container_id,
            )
        )

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

    def settle(
        self, *, user_id: str, usage_record_id: str, funding_confirmed: bool, waived: bool
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
        if not waived and (cutover.completed_at is None or not funding_confirmed):
            return None
        credited = 0
        for segment in () if waived else segments:
            for started_at, ended_at, remaining_cost in self._cost_slices(segment):
                for lot, available in self._available(
                    user_id=user_id,
                    at=started_at,
                    dimension=BilledDimension(segment.dimension),
                    container_id=(
                        segment.subject_id
                        if segment.subject_type == "container"
                        and segment.dimension == BilledDimension.ComputeRuntime.value
                        else ""
                    ),
                    consume_reserved=True,
                ):
                    amount = min(remaining_cost, available)
                    if amount <= 0:
                        break
                    self.session.add(
                        BillingCreditAllocationTable(
                            credit_lot_id=lot.id,
                            ledger_segment_id=segment.id,
                            started_at=started_at,
                            ended_at=ended_at,
                            amount_nanos=amount,
                        )
                    )
                    credited += amount
                    remaining_cost -= amount
                    if segment.subject_type == "container" and (
                        segment.dimension == BilledDimension.ComputeRuntime.value
                    ):
                        held = self.session.get(
                            BillingFundingAllocationTable,
                            (segment.subject_id, lot.id),
                        )
                        if held is not None:
                            if held.amount_nanos <= amount:
                                self.session.delete(held)
                            else:
                                held.amount_nanos -= amount
                self.session.flush()
        existing.credited_nanos = credited
        existing.payable_nanos = existing.gross_nanos - credited
        existing.settled_at = utc_now()
        self.session.flush()
        return _settlement(existing)

    def _cost_slices(
        self, segment: BillingLedgerSegmentTable
    ) -> list[tuple[datetime, datetime, int]]:
        start = to_utc(segment.segment_started_at)
        end = to_utc(segment.segment_ended_at)
        boundaries = {start, end}
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
            if not CreditScope(lot.scope).covers(BilledDimension(segment.dimension)):
                continue
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
        return {
            dimension: CreditAdjustments(
                credited_nanos=int(allocated.get(dimension.value, 0)),
                unsettled_nanos=int(pending.get(dimension.value, 0)),
            )
            for dimension in BilledDimension
        }

    def _available(
        self,
        *,
        user_id: str,
        at: datetime,
        dimension: BilledDimension,
        container_id: str = "",
        consume_reserved: bool = False,
    ) -> list[tuple[BillingCreditLotTable, int]]:
        balances = self._lot_balances(user_id=user_id, at=at, dimension=dimension)
        purchased_debt = sum(
            -amount
            for lot, amount in balances
            if lot.kind == CreditKind.Purchased.value and amount < 0
        )
        held_rows = self.session.execute(
            select(
                BillingFundingAllocationTable.credit_lot_id,
                func.sum(BillingFundingAllocationTable.amount_nanos),
            )
            .join(
                BillingCreditLotTable,
                BillingCreditLotTable.id == BillingFundingAllocationTable.credit_lot_id,
            )
            .where(
                BillingCreditLotTable.user_id == user_id,
                BillingFundingAllocationTable.container_id != container_id
                if container_id
                else true(),
            )
            .group_by(BillingFundingAllocationTable.credit_lot_id)
        ).all()
        held = {row[0]: int(row[1]) for row in held_rows}
        reserved = (
            {
                row.credit_lot_id: row.amount_nanos
                for row in self.session.scalars(
                    select(BillingFundingAllocationTable).where(
                        BillingFundingAllocationTable.container_id == container_id,
                    )
                )
            }
            if consume_reserved and container_id
            else {}
        )
        available: list[tuple[BillingCreditLotTable, int]] = []
        for lot, amount in balances:
            if lot.kind == CreditKind.Purchased.value:
                offset = min(max(0, amount), purchased_debt)
                amount -= offset
                purchased_debt -= offset
            amount -= held.get(lot.id, 0)
            # A refund cannot erase runtime already authorized against this lot.
            # Consuming its reservation records the resulting purchased-credit debt.
            if lot.kind == CreditKind.Purchased.value:
                amount = max(amount, reserved.get(lot.id, 0))
            if amount > 0:
                available.append((lot, amount))
        return available

    def _lot_balances(
        self, *, user_id: str, at: datetime, dimension: BilledDimension
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
                BillingCreditAdjustmentTable.effective_at <= utc_now(),
            )
            .group_by(
                BillingCreditAdjustmentTable.credit_lot_id,
            )
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
                or_(
                    BillingCreditLotTable.expires_at.is_(None),
                    BillingCreditLotTable.expires_at > moment,
                ),
            )
            .order_by(
                BillingCreditLotTable.expires_at.asc().nulls_last(),
                BillingCreditLotTable.kind == CreditKind.Purchased.value,
                BillingCreditLotTable.effective_at,
                BillingCreditLotTable.id,
            )
        ).all()
        return [
            (lot, int(remaining))
            for lot, remaining in rows
            if CreditScope(lot.scope).covers(dimension)
        ]

    def _lock(self, user_id: str) -> None:
        if BillingAccountRepository(self.session).get_by_user(user_id, for_update=True) is None:
            raise NotFoundError("no billing account exists for these credits")


def _settlement(row: BillingCreditSettlementTable) -> CreditSettlement:
    if row.credited_nanos is None or row.payable_nanos is None:
        raise ConflictError("credit settlement has not completed")
    return CreditSettlement(row.gross_nanos, row.credited_nanos, row.payable_nanos)


__all__ = ["BillingCreditRepository", "CreditAdjustments", "CreditCutover"]
