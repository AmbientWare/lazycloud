from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from billing.credits import fund_subscription_credits, recover_subscription_credits
from billing.rate_publication import publish_metered_rate_history
from database.context import ServiceContext
from database.repositories import billing_credits
from database.repositories.billing import BillingAccountRepository
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
from observability.usage import UsageService
from provider_stripe.billing import StripeBilling
from shared.billing_credits import CreditGrant, CreditKind
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_quotes import BilledDimension
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
from tests.workspaces import unfunded_billing_account


def test_late_expired_credit_pays_only_debt_inside_its_eligible_window(
    service_context: ServiceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    end = datetime(2027, 1, 1, tzinfo=UTC)
    now = end + timedelta(days=1)
    monkeypatch.setattr(billing_credits, "utc_now", lambda: now)
    start = end - timedelta(seconds=10)
    user_id, workspace_id = unfunded_billing_account(
        service_context, period_started_at=start, period_ended_at=end
    )
    with service_context.database.session() as session:
        credits = BillingCreditRepository(session)
        PlatformRateRepository(session).publish(
            pricing_version="late-credit",
            effective_at=start,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )
        record = UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="workspace",
            resource_id=workspace_id,
            metric=UsageMetric.NetworkEgressBytes,
            quantity=20,
            unit=UsageUnit.Bytes,
            metadata={
                METERING_WINDOW_STARTED_AT_METADATA_KEY: start.isoformat(),
                METERING_WINDOW_ENDED_AT_METADATA_KEY: (end + timedelta(seconds=10)).isoformat(),
            },
        )
        UsageRepository(session).append(record)
        BillingLedgerRepository(session).price_record(record)
        assert credits.balance(user_id=user_id, at=now) == -20
        grant = CreditGrant("late-renewal", CreditKind.Subscription, 100, start, end)
        credits.issue(user_id=user_id, grant=grant)
        credits.issue(user_id=user_id, grant=grant)
        assert credits.balance(user_id=user_id, at=now) == -10
        settlement = session.get(BillingCreditSettlementTable, record.id)
        assert settlement is not None
        assert (settlement.credited_nanos, settlement.payable_nanos) == (10, 10)
        assert session.scalar(select(func.count()).select_from(BillingMeterOutboxTable)) == 0


@pytest.mark.parametrize(
    ("paid_version", "paid_amount", "included_amount"),
    [
        (SubscriptionTermsVersion.BusinessV1, 249_000_000_000, 50_000_000_000),
        (SubscriptionTermsVersion.Business, 199_000_000_000, 100_000_000_000),
    ],
)
def test_paid_renewal_recovers_a_missing_expired_period_without_inventing_proration_terms(
    service_context: ServiceContext,
    monkeypatch: pytest.MonkeyPatch,
    paid_version: SubscriptionTermsVersion,
    paid_amount: int,
    included_amount: int,
) -> None:
    start = datetime(2027, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=30)
    now = end + timedelta(days=1)
    monkeypatch.setattr(billing_credits, "utc_now", lambda: now)
    user_id, workspace_id = unfunded_billing_account(
        service_context,
        period_started_at=start - timedelta(days=30),
        period_ended_at=start,
    )
    with service_context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(user_id)
        assert account is not None
        credits = BillingCreditRepository(session)
        PlatformRateRepository(session).publish(
            pricing_version="missing-renewal",
            effective_at=start,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )
        record = UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="workspace",
            resource_id=workspace_id,
            metric=UsageMetric.NetworkEgressBytes,
            quantity=20,
            unit=UsageUnit.Bytes,
            metadata={
                METERING_WINDOW_STARTED_AT_METADATA_KEY: (end - timedelta(seconds=10)).isoformat(),
                METERING_WINDOW_ENDED_AT_METADATA_KEY: (end + timedelta(seconds=10)).isoformat(),
            },
        )
        UsageRepository(session).append(record)
        BillingLedgerRepository(session).price_record(record)
        assert credits.balance(user_id=user_id, at=now) == -20
    subscription = ProviderSubscription(
        provider_subscription_id=account.provider_subscription_id,
        status="active",
        current_period_started_at=end,
        current_period_ended_at=end + timedelta(days=30),
        plan=BillingPlanId.Business,
        terms_version=SubscriptionTermsVersion.Business,
        scheduled_terms_version=None,
        scheduled_change_at=None,
    )
    line = ProviderPaidSubscriptionPeriod(
        provider_invoice_id="in_missing_renewal",
        provider_invoice_line_id="il_missing_renewal",
        provider_subscription_id=account.provider_subscription_id,
        plan=BillingPlanId.Business,
        terms_version=paid_version,
        period_started_at=start,
        period_ended_at=end,
        prorated=True,
        amount_nanos=paid_amount,
        invoice_paid_nanos=paid_amount,
        paid_at=now,
    )

    def receipts(
        self: StripeBilling,
        *,
        provider_customer_id: str,
        provider_subscription_id: str,
        since: datetime,
    ) -> tuple[ProviderPaidSubscriptionPeriod, ...]:
        return (line,)

    monkeypatch.setattr(StripeBilling, "paid_subscription_periods", receipts)
    with httpx.Client() as client:
        payments = StripeBilling(client)
        with service_context.database.session() as session:
            recover_subscription_credits(
                session, payments, account=account, subscription=subscription
            )
            assert (
                BillingAllowanceRepository(session).current_period(user_id=user_id, at=start)
                is None
            )
        line = line.model_copy(update={"prorated": False})
        for _ in range(2):
            with service_context.database.session() as session:
                recover_subscription_credits(
                    session, payments, account=account, subscription=subscription
                )
                assert BillingCreditRepository(session).balance(user_id=user_id, at=now) == -10
        with service_context.database.session() as session:
            lot = session.scalars(
                select(BillingCreditLotTable).where(BillingCreditLotTable.user_id == user_id)
            ).one()
            assert lot.expires_at is not None
            assert (to_utc(lot.effective_at), to_utc(lot.expires_at)) == (start, end)
            assert lot.amount_nanos == included_amount
            settlement = session.get(BillingCreditSettlementTable, record.id)
            assert settlement is not None
            assert (settlement.credited_nanos, settlement.payable_nanos) == (10, 10)
            assert session.scalar(select(func.count()).select_from(BillingMeterOutboxTable)) == 0


def test_storage_grace_waives_only_retained_time_and_top_up_resumes_charges(
    service_context: ServiceContext,
) -> None:
    start = datetime(2027, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=10)
    user_id, workspace_id = unfunded_billing_account(
        service_context,
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
    with service_context.database.session() as session:
        credits = BillingCreditRepository(session)
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
    committed_service_context: ServiceContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime(2027, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=30)
    user_id, _ = unfunded_billing_account(
        committed_service_context, period_started_at=start, period_ended_at=end
    )
    with committed_service_context.database.session() as session:
        BillingAllowanceRepository(session).set_subscription_period(
            user_id=user_id,
            period_started_at=start,
            period_ended_at=end,
            allowance_nanos=0,
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
            with committed_service_context.database.session() as session:
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
        with committed_service_context.database.session() as session:
            lots = session.scalars(
                select(BillingCreditLotTable).where(
                    BillingCreditLotTable.user_id == user_id,
                    BillingCreditLotTable.kind == CreditKind.Subscription.value,
                )
            ).all()
            assert len(lots) == 1
            assert lots[0].amount_nanos == 100_000_000_000 // (30 * 24 * 60)
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
                    "amount_nanos": 199_000_000_000,
                    "invoice_paid_nanos": 199_000_000_000,
                    "paid_at": start,
                }
            ),
        )
        with committed_service_context.database.session() as session:
            BillingAllowanceRepository(session).set_subscription_period(
                user_id=user_id,
                period_started_at=start,
                period_ended_at=end,
                allowance_nanos=0,
            )
        fund()
        fund()
        with committed_service_context.database.session() as session:
            assert (
                BillingCreditRepository(session).subscription_issued(
                    user_id=user_id,
                    period_ended_at=end,
                )
                == 100_000_000_000
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
                    "amount_nanos": 99_500_000_000,
                    "invoice_paid_nanos": 49_500_000_000,
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
                    "invoice_paid_nanos": 49_500_000_000,
                    "prorated": True,
                }
            ),
        )
        with committed_service_context.database.session() as session:
            BillingAllowanceRepository(session).set_subscription_period(
                user_id=user_id,
                period_started_at=start,
                period_ended_at=end,
                allowance_nanos=30_000_000_000,
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
        with committed_service_context.database.session() as session:
            lots = session.execute(
                select(BillingCreditLotTable.amount_nanos).where(
                    BillingCreditLotTable.user_id == user_id,
                    BillingCreditLotTable.expires_at == end,
                )
            ).all()
            assert {row[0] for row in lots} == {30_000_000_000, 35_000_000_000}


def test_credit_expiry_and_start_split_a_frozen_charge_and_preserve_purchased_funds(
    service_context: ServiceContext,
) -> None:
    at = datetime(2026, 9, 12, tzinfo=UTC)
    user_id, workspace_id = unfunded_billing_account(
        service_context, period_started_at=at, period_ended_at=at + timedelta(days=30)
    )
    with service_context.database.session() as session:
        publish_metered_rate_history(session)
        credits = BillingCreditRepository(session)
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
    UsageService(service_context).append(record)
    UsageService(service_context).append(record)
    with service_context.database.session() as session:
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


def test_waived_usage_preserves_purchased_credit_and_records_the_gross_waiver(
    service_context: ServiceContext,
) -> None:
    at = datetime(2026, 9, 12, tzinfo=UTC)
    user_id, workspace_id = unfunded_billing_account(
        service_context, period_started_at=at, period_ended_at=at + timedelta(days=30)
    )
    with service_context.database.session() as session:
        publish_metered_rate_history(session)
        account = session.scalar(
            select(BillingAccountTable).where(BillingAccountTable.user_id == user_id)
        )
        assert account is not None
        account.complimentary_since = at
        credits = BillingCreditRepository(session)
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
    UsageService(service_context).append(record)
    with service_context.database.session() as session:
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
