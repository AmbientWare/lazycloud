from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

from api.server.services import ApiServices
from billing.rate_publication import publish_metered_rate_history
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_ledger import BillingLedgerRepository
from database.tables.billing import BillingAccountTable
from database.tables.billing_credits import (
    BillingCreditAllocationTable,
    BillingCreditLotTable,
    BillingCreditSettlementTable,
)
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_outbox import BillingMeterOutboxTable
from shared.billing_credits import CreditGrant, CreditKind, CreditScope
from shared.billing_quotes import BilledDimension
from shared.billing_rate_card import METERED_RATES_EFFECTIVE_AT
from shared.timestamps import to_utc
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from sqlalchemy import func, select
from tests.service_fixtures import workspace_owner_user_id


def test_credit_expiry_and_start_split_a_frozen_charge_and_preserve_purchased_funds(
    postgres_services: ApiServices,
) -> None:
    at = datetime(2026, 9, 12, tzinfo=UTC)
    with postgres_services.context.database.session() as session:
        workspace_id = postgres_services.context.default_workspace_id(session)
        user_id = workspace_owner_user_id(postgres_services.context, workspace_id)
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=at)
        credits.complete_cutover(user_id=user_id, at=at)
        allowance = BillingAllowanceRepository(session)
        period = allowance.current_period(user_id=user_id, at=at)
        assert period is not None
        allowance.confirm_credit(user_id=user_id, period_started_at=period.started_at, at=at)
        for source, kind, scope, start, end in (
            ("purchased", CreditKind.Purchased, CreditScope.AllMetered, at, None),
            ("early", CreditKind.Trial, CreditScope.AllMetered, at, at + timedelta(seconds=5)),
            (
                "compute",
                CreditKind.Subscription,
                CreditScope.Compute,
                at,
                at + timedelta(seconds=30),
            ),
            (
                "late",
                CreditKind.Trial,
                CreditScope.AllMetered,
                at + timedelta(seconds=8),
                at + timedelta(seconds=30),
            ),
        ):
            credits.issue(
                user_id=user_id,
                grant=CreditGrant(source, kind, scope, 100_000_000, start, end),
            )
    record = UsageRecord(
        id=str(uuid4()),
        workspace_id=workspace_id,
        resource_type="workspace",
        resource_id=workspace_id,
        metric=UsageMetric.NetworkEgressBytes,
        quantity=1_073_741_824,
        unit=UsageUnit.Bytes,
        metadata={
            METERING_WINDOW_STARTED_AT_METADATA_KEY: at.isoformat(),
            METERING_WINDOW_ENDED_AT_METADATA_KEY: (at + timedelta(seconds=10)).isoformat(),
        },
    )
    postgres_services.usage.append(record)
    postgres_services.usage.append(record)
    with postgres_services.context.database.session() as session:
        allocations = session.execute(
            select(
                BillingCreditLotTable.source_id, func.sum(BillingCreditAllocationTable.amount_nanos)
            )
            .join(
                BillingCreditAllocationTable,
                BillingCreditAllocationTable.credit_lot_id == BillingCreditLotTable.id,
            )
            .group_by(BillingCreditLotTable.source_id)
        ).all()
        assert {row[0]: row[1] for row in allocations} == {
            "early": 65_000_000,
            "late": 26_000_000,
            "purchased": 39_000_000,
        }
        assert session.scalar(select(func.sum(BillingLedgerSegmentTable.cost_nanos))) == 130_000_000
        balance = BillingCreditRepository(session).balance(
            user_id=user_id, at=at + timedelta(seconds=10), dimension=BilledDimension.NetworkEgress
        )
        assert balance.purchased_nanos == 61_000_000
        assert balance.subscription_nanos == 0
        assert balance.trial_nanos == 74_000_000
        assert session.scalar(select(func.count()).select_from(BillingMeterOutboxTable)) == 0


def test_concurrent_usage_and_duplicate_receipts_cannot_spend_the_same_credit_twice(
    postgres_services: ApiServices,
) -> None:
    at = datetime(2026, 9, 12, tzinfo=UTC)
    with postgres_services.context.database.session() as session:
        workspace_id = postgres_services.context.default_workspace_id(session)
        user_id = workspace_owner_user_id(postgres_services.context, workspace_id)
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=at)
        credits.complete_cutover(user_id=user_id, at=at)
        allowance = BillingAllowanceRepository(session)
        period = allowance.current_period(user_id=user_id, at=at)
        assert period is not None
        allowance.confirm_credit(user_id=user_id, period_started_at=period.started_at, at=at)
        grant = CreditGrant(
            "payment:confirmed", CreditKind.Purchased, CreditScope.AllMetered, 200_000_000, at
        )
        lot_id = credits.issue(user_id=user_id, grant=grant)
        assert credits.issue(user_id=user_id, grant=grant) == lot_id
    records = [
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="workspace",
            resource_id=workspace_id,
            metric=UsageMetric.NetworkEgressBytes,
            quantity=1_073_741_824,
            unit=UsageUnit.Bytes,
            metadata={
                METERING_WINDOW_STARTED_AT_METADATA_KEY: at.isoformat(),
                METERING_WINDOW_ENDED_AT_METADATA_KEY: (at + timedelta(seconds=10)).isoformat(),
            },
        )
        for _ in range(2)
    ]
    barrier = Barrier(2)

    def append(record: UsageRecord) -> None:
        barrier.wait(timeout=5)
        postgres_services.usage.append(record)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(append, record) for record in records]
        for future in futures:
            future.result(timeout=15)
    for record in records:
        postgres_services.usage.append(record)
    with postgres_services.context.database.session() as session:
        assert session.scalar(select(func.sum(BillingLedgerSegmentTable.cost_nanos))) == 260_000_000
        assert (
            session.scalar(select(func.sum(BillingCreditAllocationTable.amount_nanos)))
            == 200_000_000
        )
        assert session.scalar(select(func.sum(BillingMeterOutboxTable.value_nanos))) == 60_000_000
        assert (
            BillingCreditRepository(session)
            .balance(user_id=user_id, at=at, dimension=BilledDimension.NetworkEgress)
            .available_nanos
            == 0
        )


def test_crossing_cutover_keeps_legacy_export_and_resumes_net_settlement_once(
    postgres_services: ApiServices,
) -> None:
    at = datetime(2026, 9, 12, tzinfo=UTC)
    boundary = at + timedelta(seconds=5)
    with postgres_services.context.database.session() as session:
        workspace_id = postgres_services.context.default_workspace_id(session)
        user_id = workspace_owner_user_id(postgres_services.context, workspace_id)
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=boundary)
        credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                "payment:cutover",
                CreditKind.Purchased,
                CreditScope.AllMetered,
                40_000_000,
                boundary,
            ),
        )
    record = UsageRecord(
        id=str(uuid4()),
        workspace_id=workspace_id,
        resource_type="workspace",
        resource_id=workspace_id,
        metric=UsageMetric.NetworkEgressBytes,
        quantity=1_073_741_824,
        unit=UsageUnit.Bytes,
        metadata={
            METERING_WINDOW_STARTED_AT_METADATA_KEY: at.isoformat(),
            METERING_WINDOW_ENDED_AT_METADATA_KEY: (at + timedelta(seconds=10)).isoformat(),
        },
    )
    postgres_services.usage.append(record)
    postgres_services.usage.append(record)
    with postgres_services.context.database.session() as session:
        segments = session.scalars(
            select(BillingLedgerSegmentTable).order_by(
                BillingLedgerSegmentTable.segment_started_at,
            )
        ).all()
        assert [segment.cost_nanos for segment in segments] == [65_000_000, 65_000_000]
        frozen_ids = [segment.id for segment in segments]
        settlement = session.get(BillingCreditSettlementTable, record.id)
        assert settlement is not None and settlement.settled_at is None
        assert settlement.gross_nanos == 65_000_000
        legacy = session.scalar(select(BillingMeterOutboxTable))
        assert legacy is not None
        assert legacy.identifier == record.id and legacy.value_nanos == 65_000_000
        assert to_utc(legacy.metering_ended_at) == boundary
        legacy.status = "sent"
        credits = BillingCreditRepository(session)
        credits.complete_cutover(user_id=user_id, at=boundary)
        BillingLedgerRepository(session).settle_pending_credits(owner_user_id=user_id)
        assert settlement.settled_at is None
        allowance = BillingAllowanceRepository(session)
        period = allowance.current_period(user_id=user_id, at=boundary)
        assert period is not None
        allowance.confirm_credit(user_id=user_id, period_started_at=period.started_at, at=boundary)
        BillingLedgerRepository(session).settle_pending_credits(owner_user_id=user_id)
    postgres_services.usage.append(record)
    with postgres_services.context.database.session() as session:
        rows = session.scalars(
            select(BillingMeterOutboxTable).order_by(
                BillingMeterOutboxTable.occurred_at,
            )
        ).all()
        assert [(row.status, row.value_nanos) for row in rows] == [
            ("sent", 65_000_000),
            ("pending", 25_000_000),
        ]
        assert all(row.usage_record_id == record.id for row in rows)
        assert to_utc(rows[1].occurred_at) == boundary
        assert (
            list(
                session.scalars(
                    select(BillingLedgerSegmentTable.id).order_by(
                        BillingLedgerSegmentTable.segment_started_at,
                    )
                )
            )
            == frozen_ids
        )


def test_waived_usage_preserves_purchased_credit_and_records_the_gross_waiver(
    postgres_services: ApiServices,
) -> None:
    at = datetime(2026, 9, 12, tzinfo=UTC)
    with postgres_services.context.database.session() as session:
        workspace_id = postgres_services.context.default_workspace_id(session)
        user_id = workspace_owner_user_id(postgres_services.context, workspace_id)
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
        account = session.scalar(
            select(BillingAccountTable).where(BillingAccountTable.user_id == user_id)
        )
        assert account is not None
        account.complimentary_since = at
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=at)
        credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                "payment:waived",
                CreditKind.Purchased,
                CreditScope.AllMetered,
                100_000_000,
                at,
            ),
        )
    record = UsageRecord(
        id=str(uuid4()),
        workspace_id=workspace_id,
        resource_type="workspace",
        resource_id=workspace_id,
        metric=UsageMetric.NetworkEgressBytes,
        quantity=1_073_741_824,
        unit=UsageUnit.Bytes,
        metadata={
            METERING_WINDOW_STARTED_AT_METADATA_KEY: at.isoformat(),
            METERING_WINDOW_ENDED_AT_METADATA_KEY: (at + timedelta(seconds=10)).isoformat(),
        },
    )
    postgres_services.usage.append(record)
    with postgres_services.context.database.session() as session:
        settlement = session.get(BillingCreditSettlementTable, record.id)
        assert settlement is not None
        assert (settlement.gross_nanos, settlement.credited_nanos, settlement.payable_nanos) == (
            130_000_000,
            0,
            130_000_000,
        )
        outbox = session.scalar(select(BillingMeterOutboxTable))
        assert outbox is not None and outbox.status == "waived"
        assert outbox.value_nanos == 130_000_000
        assert (
            BillingCreditRepository(session)
            .balance(
                user_id=user_id,
                at=at,
                dimension=BilledDimension.NetworkEgress,
            )
            .purchased_nanos
            == 100_000_000
        )
