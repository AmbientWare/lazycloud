from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from database.tables.billing_credits import (
    BillingCreditAllocationTable,
    BillingCreditCutoverTable,
    BillingCreditLotTable,
    BillingCreditSettlementTable,
)
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_outbox import BillingMeterOutboxTable
from database.tables.observability import UsageRecordTable
from database.tables.storage_access import StorageAccessTable
from shared.billing_credits import CreditKind
from shared.billing_quotes import BILLED_METRICS, LedgerBasis, LedgerComponent
from shared.storage_access import StorageRequestClass, StorageTransferEvidence
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class EconomicsUsageTotal:
    billing_owner: str
    component: LedgerComponent
    basis: LedgerBasis
    quantity: Decimal
    gross_nanos: int


@dataclass(frozen=True, slots=True)
class StorageAccessTotal:
    request_class: StorageRequestClass
    transfer_evidence: StorageTransferEvidence
    attributed: bool
    requests: int
    response_bytes: int
    missing_bytes: int


@dataclass(frozen=True, slots=True)
class EconomicsLedgerFacts:
    usage: tuple[EconomicsUsageTotal, ...]
    credits: dict[CreditKind, int]
    waived_nanos: int
    pending_meter_nanos: int
    abandoned_meter_nanos: int
    purchased_redemptions_booked_nanos: int
    crossing_segments: int
    boundary_overlap_gross_nanos: int
    unpriced_records: int
    unsettled_records: int
    legacy_segments: int
    settlement_disagreements: int
    missing_delivery_records: int
    storage_access: tuple[StorageAccessTotal, ...]


@dataclass(slots=True)
class EconomicsRepository:
    session: Session

    def read(self, *, started_at: datetime, ended_at: datetime) -> EconomicsLedgerFacts:
        ledger = BillingLedgerSegmentTable
        allocation = BillingCreditAllocationTable
        lot = BillingCreditLotTable
        settlement = BillingCreditSettlementTable
        cutover = BillingCreditCutoverTable
        attributed = (
            ledger.segment_started_at >= started_at,
            ledger.segment_started_at < ended_at,
        )
        usage = tuple(
            EconomicsUsageTotal(
                owner, LedgerComponent(component), LedgerBasis(basis), quantity, int(cost)
            )
            for owner, component, basis, quantity, cost in self.session.execute(
                select(
                    ledger.billing_owner,
                    ledger.component,
                    ledger.basis,
                    func.sum(ledger.quantity),
                    func.sum(ledger.cost_nanos),
                )
                .where(*attributed)
                .group_by(ledger.billing_owner, ledger.component, ledger.basis)
            )
        )
        credits = {
            CreditKind(kind): int(amount)
            for kind, amount in self.session.execute(
                select(lot.kind, func.sum(allocation.amount_nanos))
                .join(allocation, allocation.credit_lot_id == lot.id)
                .join(ledger, ledger.id == allocation.ledger_segment_id)
                .where(*attributed)
                .group_by(lot.kind)
            )
        }
        included_records = select(ledger.usage_record_id).where(*attributed).distinct()
        local_segments = (
            ledger.usage_record_id.in_(included_records),
            ledger.segment_started_at >= cutover.effective_at,
        )
        record_totals = (
            select(ledger.usage_record_id.label("id"), func.sum(ledger.cost_nanos).label("gross"))
            .join(cutover, cutover.user_id == ledger.owner_user_id)
            .where(*local_segments)
            .group_by(ledger.usage_record_id)
            .subquery()
        )
        allocation_totals = (
            select(
                ledger.usage_record_id.label("id"),
                func.sum(allocation.amount_nanos).label("credit"),
            )
            .join(allocation, allocation.ledger_segment_id == ledger.id)
            .join(cutover, cutover.user_id == ledger.owner_user_id)
            .where(*local_segments)
            .group_by(ledger.usage_record_id)
            .subquery()
        )
        unsettled = (
            self.session.scalar(
                select(func.count())
                .select_from(record_totals)
                .outerjoin(settlement, settlement.usage_record_id == record_totals.c.id)
                .where(settlement.settled_at.is_(None))
            )
            or 0
        )
        disagreements = (
            self.session.scalar(
                select(func.count())
                .select_from(record_totals)
                .join(settlement, settlement.usage_record_id == record_totals.c.id)
                .outerjoin(allocation_totals, allocation_totals.c.id == record_totals.c.id)
                .where(
                    settlement.settled_at.is_not(None),
                    or_(
                        settlement.gross_nanos != record_totals.c.gross,
                        settlement.credited_nanos != func.coalesce(allocation_totals.c.credit, 0),
                    ),
                )
            )
            or 0
        )
        outbox = BillingMeterOutboxTable
        delivery_window = and_(
            outbox.usage_record_id == ledger.usage_record_id,
            outbox.occurred_at <= ledger.segment_started_at,
            outbox.metering_ended_at >= ledger.segment_ended_at,
        )
        segment_allocations = (
            select(
                allocation.ledger_segment_id.label("id"),
                func.sum(allocation.amount_nanos).label("credit"),
            )
            .group_by(allocation.ledger_segment_id)
            .subquery()
        )
        meter_totals = {
            status: int(amount)
            for status, amount in self.session.execute(
                select(
                    outbox.status,
                    func.sum(ledger.cost_nanos - func.coalesce(segment_allocations.c.credit, 0)),
                )
                .select_from(ledger)
                .join(outbox, delivery_window)
                .outerjoin(segment_allocations, segment_allocations.c.id == ledger.id)
                .where(*attributed)
                .group_by(outbox.status)
            )
        }
        missing_delivery = (
            self.session.scalar(
                select(func.count())
                .select_from(ledger)
                .outerjoin(segment_allocations, segment_allocations.c.id == ledger.id)
                .outerjoin(outbox, delivery_window)
                .where(
                    *attributed,
                    ledger.cost_nanos > func.coalesce(segment_allocations.c.credit, 0),
                    outbox.id.is_(None),
                )
            )
            or 0
        )
        booked = (
            self.session.scalar(
                select(func.sum(allocation.amount_nanos))
                .join(lot, lot.id == allocation.credit_lot_id)
                .where(
                    lot.kind == CreditKind.Purchased.value,
                    allocation.created_at >= started_at,
                    allocation.created_at < ended_at,
                )
            )
            or 0
        )
        crossing, boundary_gross = self.session.execute(
            select(func.count(), func.coalesce(func.sum(ledger.cost_nanos), 0))
            .select_from(ledger)
            .where(
                ledger.segment_started_at < ended_at,
                ledger.segment_ended_at > started_at,
                or_(ledger.segment_started_at < started_at, ledger.segment_ended_at > ended_at),
            )
        ).one()
        unpriced = (
            self.session.scalar(
                select(func.count())
                .select_from(UsageRecordTable)
                .where(
                    UsageRecordTable.created_at >= started_at,
                    UsageRecordTable.created_at < ended_at,
                    UsageRecordTable.quantity > 0,
                    UsageRecordTable.metric.in_([metric.value for metric in BILLED_METRICS]),
                    ~select(ledger.id)
                    .where(ledger.usage_record_id == UsageRecordTable.id)
                    .exists(),
                )
            )
            or 0
        )
        legacy = (
            self.session.scalar(
                select(func.count())
                .select_from(ledger)
                .outerjoin(cutover, cutover.user_id == ledger.owner_user_id)
                .where(
                    *attributed,
                    or_(
                        cutover.user_id.is_(None), ledger.segment_started_at < cutover.effective_at
                    ),
                )
            )
            or 0
        )
        return EconomicsLedgerFacts(
            usage=usage,
            credits=credits,
            waived_nanos=meter_totals.get("waived", 0),
            pending_meter_nanos=meter_totals.get("pending", 0) + meter_totals.get("sending", 0),
            abandoned_meter_nanos=meter_totals.get("abandoned", 0),
            purchased_redemptions_booked_nanos=int(booked),
            crossing_segments=crossing,
            boundary_overlap_gross_nanos=int(boundary_gross),
            unpriced_records=unpriced,
            unsettled_records=unsettled,
            legacy_segments=legacy,
            settlement_disagreements=disagreements,
            missing_delivery_records=missing_delivery,
            storage_access=tuple(
                StorageAccessTotal(
                    StorageRequestClass(kind),
                    StorageTransferEvidence(evidence),
                    attributed,
                    count,
                    int(size),
                    missing,
                )
                for kind, evidence, attributed, count, size, missing in self.session.execute(
                    select(
                        StorageAccessTable.request_class,
                        StorageAccessTable.transfer_evidence,
                        StorageAccessTable.workspace_id.is_not(None),
                        func.count(),
                        func.coalesce(func.sum(StorageAccessTable.response_bytes), 0),
                        func.count().filter(StorageAccessTable.response_bytes.is_(None)),
                    )
                    .where(
                        StorageAccessTable.occurred_at >= started_at,
                        StorageAccessTable.occurred_at < ended_at,
                    )
                    .group_by(
                        StorageAccessTable.request_class,
                        StorageAccessTable.transfer_evidence,
                        StorageAccessTable.workspace_id.is_not(None),
                    )
                )
            ),
        )
