from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from api.server.services import ApiServices
from billing.credits import fund_subscription_credits
from billing.rate_publication import publish_metered_rate_history
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.billing_rates import PlatformRateRepository
from database.repositories.observability import UsageRepository
from database.repositories.storage_retention import StorageRetentionRepository
from database.tables.billing import BillingAccountTable
from database.tables.billing_credits import (
    BillingCreditAllocationTable,
    BillingCreditLotTable,
    BillingCreditSettlementTable,
)
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_outbox import BillingMeterOutboxTable
from provider_stripe.billing import StripeBilling
from shared.billing_credits import CreditGrant, CreditKind
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_quotes import BilledDimension
from shared.billing_rate_card import METERED_RATES_EFFECTIVE_AT
from shared.payments import ProviderPaidSubscriptionPeriod, ProviderSubscription
from shared.timestamps import to_utc
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from sqlalchemy import func, select
from tests.service_fixtures import legacy_billing_account


def test_storage_grace_waives_only_retained_time_and_top_up_resumes_charges(
    postgres_services: ApiServices,
) -> None:
    start = datetime(2027, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=10)
    user_id, workspace_id = legacy_billing_account(
        postgres_services.context,
        period_started_at=start,
        period_ended_at=start + timedelta(days=30),
    )
    record = UsageRecord(
        id=str(uuid4()),
        workspace_id=workspace_id,
        resource_type="volume",
        resource_id="retained",
        metric=UsageMetric.PersistentVolumeByteSeconds,
        quantity=10,
        unit=UsageUnit.ByteSeconds,
        metadata={
            METERING_WINDOW_STARTED_AT_METADATA_KEY: start.isoformat(),
            METERING_WINDOW_ENDED_AT_METADATA_KEY: end.isoformat(),
        },
        created_at=end,
    )
    with postgres_services.context.database.session() as session:
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=start)
        credits.complete_cutover(user_id=user_id, at=start)
        BillingAllowanceRepository(session).confirm_credit(
            user_id=user_id, period_started_at=start, at=start
        )
        PlatformRateRepository(session).publish(
            pricing_version="retention",
            effective_at=start,
            nanos_per_egress_byte=Decimal(0),
            nanos_per_volume_byte_second=Decimal(1),
        )
        retention = StorageRetentionRepository(session)
        period = retention.start(user_id=user_id, at=start + timedelta(seconds=2))
        retention.close(period, at=start + timedelta(seconds=7))
        credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                "payment:retention",
                CreditKind.Purchased,
                100,
                start + timedelta(seconds=7),
            ),
        )
        UsageRepository(session).append_storage(record)
        ledger = BillingLedgerRepository(session)
        ledger.price_record(record)
        ledger.price_record(record)
        settlement = session.get(BillingCreditSettlementTable, record.id)
        assert settlement is not None
        assert (settlement.gross_nanos, settlement.credited_nanos, settlement.payable_nanos) == (
            10,
            3,
            2,
        )
        assert settlement.waived_nanos == 5
        outbox = session.scalars(
            select(BillingMeterOutboxTable).where(
                BillingMeterOutboxTable.usage_record_id == record.id
            )
        ).all()
        assert outbox == []
        assert credits.balance(user_id=user_id, at=end) == 95
        totals = credits.account_adjustments(user_id=user_id, start=start, end=end)
        assert totals[BilledDimension.VolumeStorage].waived_nanos == 5


def test_paid_proration_funds_only_covered_credit_and_preserves_legacy_lots(
    postgres_services: ApiServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime(2027, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=30)
    user_id, _ = legacy_billing_account(
        postgres_services.context, period_started_at=start, period_ended_at=end
    )
    with postgres_services.context.database.session() as session:
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=start)
        credits.complete_cutover(user_id=user_id, at=start)
        BillingAllowanceRepository(session).set_subscription_period(
            user_id=user_id,
            period_started_at=start,
            period_ended_at=end,
            allowance_nanos=0,
            funded=False,
        )
    subscription = ProviderSubscription(
        provider_subscription_id="sub_proration",
        status="active",
        current_period_started_at=start,
        current_period_ended_at=end,
        plan=BillingPlanId.Business,
        terms_version=SubscriptionTermsVersion.Business,
        scheduled_terms_version=None,
        scheduled_change_at=None,
    )
    evidence = (
        ProviderPaidSubscriptionPeriod(
            provider_invoice_id="in_last_minute",
            provider_invoice_line_id="il_business",
            provider_subscription_id=subscription.provider_subscription_id,
            plan=BillingPlanId.Business,
            terms_version=SubscriptionTermsVersion.Business,
            period_started_at=end - timedelta(minutes=1),
            period_ended_at=end,
            prorated=True,
            amount_nanos=10_000_000,
            invoice_paid_nanos=10_000_000,
            paid_at=end - timedelta(minutes=1),
        ),
    )

    def receipts(
        self: StripeBilling,
        *,
        provider_customer_id: str,
        provider_subscription_id: str,
        since: datetime,
    ) -> tuple[ProviderPaidSubscriptionPeriod, ...]:
        return evidence

    monkeypatch.setattr(StripeBilling, "paid_subscription_periods", receipts)
    with httpx.Client() as client:
        payments = StripeBilling(client)

        def fund() -> None:
            with postgres_services.context.database.session() as session:
                assert fund_subscription_credits(
                    session,
                    payments,
                    user_id=user_id,
                    provider_customer_id="cus_proration",
                    subscription=subscription,
                )

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(fund) for _ in range(4)]
            for future in futures:
                future.result()
        with postgres_services.context.database.session() as session:
            lots = session.scalars(
                select(BillingCreditLotTable).where(
                    BillingCreditLotTable.user_id == user_id,
                    BillingCreditLotTable.kind == CreditKind.Subscription.value,
                )
            ).all()
            assert len(lots) == 1
            assert lots[0].amount_nanos == 50_000_000_000 // (30 * 24 * 60)
            period = BillingAllowanceRepository(session).current_period(user_id=user_id, at=start)
            assert period is not None and period.allowance_nanos == lots[0].amount_nanos

        start, end = end, end + timedelta(days=30)
        subscription = subscription.model_copy(
            update={
                "current_period_started_at": start,
                "current_period_ended_at": end,
            }
        )
        evidence = (
            evidence[0].model_copy(
                update={
                    "provider_invoice_id": "in_renewal",
                    "provider_invoice_line_id": "il_renewal",
                    "period_started_at": start,
                    "period_ended_at": end,
                    "prorated": False,
                    "amount_nanos": 249_000_000_000,
                    "invoice_paid_nanos": 249_000_000_000,
                    "paid_at": start,
                }
            ),
        )
        with postgres_services.context.database.session() as session:
            BillingAllowanceRepository(session).set_subscription_period(
                user_id=user_id,
                period_started_at=start,
                period_ended_at=end,
                allowance_nanos=0,
                funded=False,
            )
        fund()
        fund()
        with postgres_services.context.database.session() as session:
            assert (
                BillingCreditRepository(session).subscription_issued(
                    user_id=user_id,
                    period_ended_at=end,
                )
                == 50_000_000_000
            )

        start, end = end, end + timedelta(days=30)
        middle = start + timedelta(days=15)
        subscription = subscription.model_copy(
            update={
                "current_period_started_at": start,
                "current_period_ended_at": end,
            }
        )
        evidence = (
            evidence[0].model_copy(
                update={
                    "provider_invoice_id": "in_legacy",
                    "provider_invoice_line_id": "il_legacy",
                    "plan": BillingPlanId.Team,
                    "terms_version": SubscriptionTermsVersion.TeamLegacy,
                    "period_started_at": start,
                    "period_ended_at": end,
                    "paid_at": start,
                    "amount_nanos": 100_000_000_000,
                    "invoice_paid_nanos": 100_000_000_000,
                }
            ),
            evidence[0].model_copy(
                update={
                    "provider_invoice_id": "in_upgrade",
                    "provider_invoice_line_id": "il_new",
                    "period_started_at": middle,
                    "period_ended_at": end,
                    "paid_at": middle,
                    "amount_nanos": 124_500_000_000,
                    "invoice_paid_nanos": 74_500_000_000,
                    "prorated": True,
                }
            ),
            evidence[0].model_copy(
                update={
                    "provider_invoice_id": "in_upgrade",
                    "provider_invoice_line_id": "il_old",
                    "plan": BillingPlanId.Team,
                    "terms_version": SubscriptionTermsVersion.TeamLegacy,
                    "period_started_at": middle,
                    "period_ended_at": end,
                    "paid_at": middle,
                    "amount_nanos": -50_000_000_000,
                    "invoice_paid_nanos": 74_500_000_000,
                    "prorated": True,
                }
            ),
        )
        with postgres_services.context.database.session() as session:
            BillingAllowanceRepository(session).set_subscription_period(
                user_id=user_id,
                period_started_at=start,
                period_ended_at=end,
                allowance_nanos=30_000_000_000,
                funded=True,
            )
            BillingCreditRepository(session).issue(
                user_id=user_id,
                grant=CreditGrant(
                    source_id="subscription:in_legacy:il_legacy",
                    kind=CreditKind.Subscription,
                    amount_nanos=30_000_000_000,
                    effective_at=start,
                    expires_at=end,
                ),
            )
        fund()
        fund()
        with postgres_services.context.database.session() as session:
            lots = session.execute(
                select(BillingCreditLotTable.amount_nanos).where(
                    BillingCreditLotTable.user_id == user_id,
                    BillingCreditLotTable.expires_at == end,
                )
            ).all()
            assert {row[0] for row in lots} == {30_000_000_000, 10_000_000_000}


def test_credit_expiry_and_start_split_a_frozen_charge_and_preserve_purchased_funds(
    postgres_services: ApiServices,
) -> None:
    at = datetime(2026, 9, 12, tzinfo=UTC)
    user_id, workspace_id = legacy_billing_account(
        postgres_services.context, period_started_at=at, period_ended_at=at + timedelta(days=30)
    )
    with postgres_services.context.database.session() as session:
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=at)
        credits.complete_cutover(user_id=user_id, at=at)
        allowance = BillingAllowanceRepository(session)
        period = allowance.current_period(user_id=user_id, at=at)
        assert period is not None
        allowance.confirm_credit(user_id=user_id, period_started_at=period.started_at, at=at)
        for source, kind, start, end in (
            ("purchased", CreditKind.Purchased, at, None),
            ("early", CreditKind.Trial, at, at + timedelta(seconds=5)),
            (
                "compute",
                CreditKind.Subscription,
                at,
                at + timedelta(seconds=30),
            ),
            (
                "late",
                CreditKind.Trial,
                at + timedelta(seconds=8),
                at + timedelta(seconds=30),
            ),
        ):
            credits.issue(
                user_id=user_id,
                grant=CreditGrant(source, kind, 100_000_000, start, end),
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
            "compute": 65_000_000,
        }
        assert session.scalar(select(func.sum(BillingLedgerSegmentTable.cost_nanos))) == 130_000_000
        balance = BillingCreditRepository(session).balance(
            user_id=user_id, at=at + timedelta(seconds=10)
        )
        assert balance == 235_000_000
        assert session.scalar(select(func.count()).select_from(BillingMeterOutboxTable)) == 0


def test_crossing_cutover_keeps_legacy_export_and_resumes_net_settlement_once(
    postgres_services: ApiServices,
) -> None:
    at = datetime(2026, 9, 12, tzinfo=UTC)
    boundary = at + timedelta(seconds=5)
    user_id, workspace_id = legacy_billing_account(
        postgres_services.context, period_started_at=at, period_ended_at=at + timedelta(days=30)
    )
    with postgres_services.context.database.session() as session:
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=boundary)
        credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                "payment:cutover",
                CreditKind.Purchased,
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
        assert settlement.settled_at is not None
    postgres_services.usage.append(record)
    with postgres_services.context.database.session() as session:
        rows = session.scalars(
            select(BillingMeterOutboxTable).order_by(
                BillingMeterOutboxTable.occurred_at,
            )
        ).all()
        assert [(row.status, row.value_nanos) for row in rows] == [
            ("sent", 65_000_000),
        ]
        assert all(row.usage_record_id == record.id for row in rows)
        assert BillingCreditRepository(session).balance(user_id=user_id, at=boundary) == -25_000_000
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
    user_id, workspace_id = legacy_billing_account(
        postgres_services.context, period_started_at=at, period_ended_at=at + timedelta(days=30)
    )
    with postgres_services.context.database.session() as session:
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
            0,
        )
        assert settlement.waived_nanos == 130_000_000
        assert session.scalar(select(BillingMeterOutboxTable)) is None
        assert (
            BillingCreditRepository(session).balance(
                user_id=user_id,
                at=at,
            )
            == 100_000_000
        )
