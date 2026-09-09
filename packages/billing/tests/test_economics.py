from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from billing.economics import (
    EconomicsPeriod,
    EconomicsService,
    EconomicsStatement,
    EconomicsStatus,
    StatementComponent,
    StatementEntry,
)
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.billing_rates import PlatformRateRepository
from database.repositories.identity import (
    UserRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from database.repositories.observability import UsageRepository
from database.tables.billing_credits import (
    BillingCreditAllocationTable,
    BillingCreditCutoverTable,
    BillingCreditLotTable,
    BillingCreditSettlementTable,
)
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_outbox import BillingMeterOutboxTable
from database.tables.observability import UsageRecordTable
from pydantic import ValidationError
from shared.billing_accounts import BillingAccountStatus
from shared.billing_credits import CreditGrant, CreditKind
from shared.timestamps import utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from sqlalchemy.engine import URL

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


def test_refunded_spent_credit_and_promotional_debt_payment_reconcile(
    migrated_database_url: URL,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=migrated_database_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    start = utc_now() - timedelta(seconds=2)
    try:
        with database.session() as session:
            user = UserRepository(session).create(display_name="refund economics")
            workspace = WorkspaceRepository(session).create(name="refund economics")
            WorkspaceMemberRepository(session).ensure_owner(
                workspace_id=workspace.id, user_id=user.id
            )
            user_id = user.id
            BillingAccountRepository(session).upsert(
                user_id=user_id,
                status=BillingAccountStatus.Active,
                provider_customer_id="refund-economics",
                provider_subscription_id="",
                provider_credit_grant_id="",
                plan=None,
                subscription_terms_version=None,
                scheduled_terms_version=None,
                scheduled_change_at=None,
            )
            credits = BillingCreditRepository(session)
            credits.prepare_cutover(user_id=user_id, effective_at=start)
            credits.complete_cutover(user_id=user_id, at=start)
            lot = credits.issue(
                user_id=user_id, grant=CreditGrant("purchase", CreditKind.Purchased, 20, start)
            )
            PlatformRateRepository(session).publish(
                pricing_version="refund-economics",
                effective_at=start,
                nanos_per_egress_byte=Decimal(1),
                nanos_per_volume_byte_second=Decimal(0),
            )
            record = UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace.id,
                resource_type="workspace",
                resource_id=workspace.id,
                metric=UsageMetric.NetworkEgressBytes,
                quantity=20,
                unit=UsageUnit.Bytes,
                metadata={
                    METERING_WINDOW_STARTED_AT_METADATA_KEY: start.isoformat(),
                    METERING_WINDOW_ENDED_AT_METADATA_KEY: (
                        start + timedelta(seconds=1)
                    ).isoformat(),
                },
            )
            UsageRepository(session).append(record)
            BillingLedgerRepository(session).price_record(record)
            credits.adjust(
                user_id=user_id,
                credit_lot_id=lot,
                source_id="refund",
                amount_nanos=-20,
                effective_at=utc_now(),
            )
            assert credits.balance(user_id=user_id, at=utc_now()) == -20
        service = EconomicsService(database)
        period = EconomicsPeriod(started_at=start, ended_at=utc_now())
        amounts = {component: 0 for component in StatementComponent}
        amounts.update(
            {
                StatementComponent.GrossUsageControl: 20,
                StatementComponent.PrepaidCashReceived: 20,
                StatementComponent.PrepaidCashRefunded: 20,
                StatementComponent.PurchasedCreditClosingNetBalance: -20,
            }
        )
        statement = EconomicsStatement(
            **period.model_dump(),
            attribution_basis="segment_started_at",
            entries=[
                StatementEntry(component=component, amount_nanos=amount, reference=component.value)
                for component, amount in amounts.items()
            ],
        )
        assert service.report(period, statement=statement).reconciliation_gaps == []
        next_start = utc_now()
        with database.session() as session:
            credits = BillingCreditRepository(session)
            credits.issue(
                user_id=user_id,
                grant=CreditGrant(
                    "promotion", CreditKind.Trial, 10, next_start, next_start + timedelta(days=1)
                ),
            )
            assert credits.balance(user_id=user_id, at=utc_now()) == -10
        period = EconomicsPeriod(started_at=next_start, ended_at=utc_now())
        amounts = {component: 0 for component in StatementComponent}
        amounts.update(
            {
                StatementComponent.PurchasedCreditOpeningNetBalance: -20,
                StatementComponent.PurchasedCreditClosingNetBalance: -10,
            }
        )
        statement = EconomicsStatement(
            **period.model_dump(),
            attribution_basis="segment_started_at",
            entries=[
                StatementEntry(component=component, amount_nanos=amount, reference=component.value)
                for component, amount in amounts.items()
            ],
        )
        report = service.report(period, statement=statement)
        assert report.reconciliation_gaps == []
        assert report.purchased_credit_offsets_booked_nanos == 10
        assert report.net_revenue_nanos == -10
    finally:
        database.dispose()


def test_report_keeps_cash_out_of_revenue_and_requires_reconciled_actual_costs(
    migrated_database_url: URL,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=migrated_database_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    start = datetime.now(UTC) - timedelta(days=2)
    end = start + timedelta(hours=1)
    period = EconomicsPeriod(started_at=start, ended_at=end)
    gross = 9_007_199_254_740_999
    usage_id, segment_id = str(uuid4()), str(uuid4())
    try:
        with database.session() as session:
            user = UserRepository(session).create(display_name="economics")
            workspace = WorkspaceRepository(session).create(name="economics")
            session.add(
                BillingCreditCutoverTable(
                    user_id=user.id,
                    effective_at=start,
                    completed_at=start,
                )
            )
            session.add(
                UsageRecordTable(
                    id=usage_id,
                    workspace_id=workspace.id,
                    resource_type="container",
                    resource_id=str(uuid4()),
                    metric="container_duration_milliseconds",
                    quantity=1000,
                    payload={},
                    created_at=start,
                    updated_at=start,
                )
            )
            session.flush()
            session.add(
                BillingLedgerSegmentTable(
                    id=segment_id,
                    usage_record_id=usage_id,
                    segment_index=0,
                    workspace_id=workspace.id,
                    owner_user_id=user.id,
                    dimension="compute_runtime",
                    component="cpu",
                    basis="reserved",
                    subject_type="container",
                    subject_id="task",
                    billing_owner="platform_fleet",
                    span_started_at=start,
                    span_ended_at=end + timedelta(minutes=1),
                    segment_started_at=start,
                    segment_ended_at=end + timedelta(seconds=1),
                    duration_ms=3_601_000,
                    quantity=Decimal("3601.000000001"),
                    pricing_version="economics-acceptance",
                    rate_nanos_per_unit=Decimal(1),
                    quote_effective_at=start,
                    cost_nanos=gross,
                )
            )
            session.add(
                BillingLedgerSegmentTable(
                    id=str(uuid4()),
                    usage_record_id=usage_id,
                    segment_index=1,
                    workspace_id=workspace.id,
                    owner_user_id=user.id,
                    dimension="compute_runtime",
                    component="cpu",
                    basis="reserved",
                    subject_type="container",
                    subject_id="task",
                    billing_owner="platform_fleet",
                    span_started_at=start,
                    span_ended_at=end + timedelta(minutes=1),
                    segment_started_at=end + timedelta(seconds=1),
                    segment_ended_at=end + timedelta(minutes=1),
                    duration_ms=59_000,
                    quantity=Decimal(59),
                    pricing_version="economics-acceptance",
                    rate_nanos_per_unit=Decimal(1),
                    quote_effective_at=start,
                    cost_nanos=7,
                )
            )
            session.flush()
            for kind, amount in (
                (CreditKind.Trial, 101),
                (CreditKind.Subscription, 203),
                (CreditKind.Purchased, 307),
            ):
                lot_id = str(uuid4())
                session.add(
                    BillingCreditLotTable(
                        id=lot_id,
                        user_id=user.id,
                        source_id=kind.value,
                        kind=kind.value,
                        amount_nanos=amount,
                        effective_at=start,
                    )
                )
                session.flush()
                session.add(
                    BillingCreditAllocationTable(
                        id=str(uuid4()),
                        credit_lot_id=lot_id,
                        ledger_segment_id=segment_id,
                        started_at=start,
                        ended_at=end,
                        amount_nanos=amount,
                        created_at=start + timedelta(minutes=1),
                    )
                )
            session.add(
                BillingCreditSettlementTable(
                    usage_record_id=usage_id,
                    user_id=user.id,
                    gross_nanos=gross + 7,
                    credited_nanos=611,
                    payable_nanos=gross + 7 - 611,
                    settled_at=start,
                )
            )
            session.add(
                BillingLedgerSegmentTable(
                    id=str(uuid4()),
                    usage_record_id=usage_id,
                    segment_index=2,
                    workspace_id=workspace.id,
                    owner_user_id=user.id,
                    dimension="compute_runtime",
                    component="cpu",
                    basis="reserved",
                    subject_type="container",
                    subject_id="task",
                    billing_owner="platform_fleet",
                    span_started_at=start - timedelta(minutes=1),
                    span_ended_at=end + timedelta(minutes=1),
                    segment_started_at=start - timedelta(minutes=1),
                    segment_ended_at=start,
                    duration_ms=60_000,
                    quantity=Decimal(60),
                    pricing_version="economics-acceptance",
                    rate_nanos_per_unit=Decimal(1),
                    quote_effective_at=start - timedelta(minutes=1),
                    cost_nanos=999,
                )
            )
            session.add(
                BillingMeterOutboxTable(
                    workspace_id=workspace.id,
                    usage_record_id=usage_id,
                    identifier=str(uuid4()),
                    provider_customer_id="economics",
                    meter_event_name="compute",
                    value_nanos=999,
                    pricing_version="economics",
                    occurred_at=start - timedelta(minutes=1),
                    metering_ended_at=start,
                    status="waived",
                    next_attempt_at=start,
                )
            )
        service = EconomicsService(database)
        incomplete = service.report(period)
        assert incomplete.status is EconomicsStatus.Incomplete
        assert incomplete.exit_code == 2
        assert incomplete.operating_margin is None
        assert incomplete.gross_usage_nanos == gross
        amounts = {
            StatementComponent.GrossUsageControl: gross,
            StatementComponent.SubscriptionRevenue: 1000,
            StatementComponent.RefundsAndWriteoffs: 0,
            StatementComponent.SupplierCompute: gross - 1000,
            StatementComponent.ObjectStorage: 10,
            StatementComponent.ObjectOperations: 20,
            StatementComponent.Network: 30,
            StatementComponent.PaymentFees: 40,
            StatementComponent.FixedInfrastructure: 100,
            StatementComponent.PrepaidCashReceived: 9999,
            StatementComponent.PrepaidCashRefunded: 0,
            StatementComponent.PurchasedCreditOpeningNetBalance: 100,
            StatementComponent.PurchasedCreditClosingNetBalance: 9792,
        }
        statement = EconomicsStatement(
            **period.model_dump(),
            attribution_basis="segment_started_at",
            entries=[
                StatementEntry(
                    component=component,
                    amount_nanos=amount,
                    reference=f"statement/{component.value}",
                )
                for component, amount in amounts.items()
            ],
        )
        result = service.report(period, statement=statement)
        assert result.status is EconomicsStatus.Profitable
        assert result.exit_code == 0
        assert result.net_revenue_nanos == gross + 696
        assert result.operating_result_nanos == 1496
        assert result.operating_margin == Decimal(1496) / Decimal(gross + 696)
        assert result.crossing_segments == 1
        assert result.boundary_overlap_gross_nanos == gross
        assert result.waived_nanos == 0
        with_legacy = service.report(
            EconomicsPeriod(started_at=start - timedelta(minutes=1), ended_at=end)
        )
        assert with_legacy.gross_usage_nanos == gross + 999
        assert with_legacy.waived_nanos == 999
        assert with_legacy.reconciliation_gaps == [
            "1 legacy segments lack local credit attribution"
        ]
        assert (
            result.purchased_credit_applied_nanos
            == result.purchased_redemptions_booked_nanos
            == 307
        )
        assert result.occupancy.cpu_reservation_ratio is None
        without_fees = statement.model_copy(
            update={
                "entries": [
                    entry
                    for entry in statement.entries
                    if entry.component is not StatementComponent.PaymentFees
                ]
            }
        )
        assert service.report(period, statement=without_fees).contribution_margin is None
        bad_liability = statement.model_copy(
            update={
                "entries": [
                    entry.model_copy(update={"amount_nanos": 1})
                    if entry.component is StatementComponent.PurchasedCreditClosingNetBalance
                    else entry
                    for entry in statement.entries
                ]
            }
        )
        assert service.report(period, statement=bad_liability).status is EconomicsStatus.Incomplete
        for component, cost, status in (
            (StatementComponent.FixedInfrastructure, 2000, EconomicsStatus.OperatingLoss),
            (StatementComponent.SupplierCompute, gross + 1000, EconomicsStatus.ContributionLoss),
        ):
            loss = statement.model_copy(
                update={
                    "entries": [
                        entry.model_copy(update={"amount_nanos": cost})
                        if entry.component is component
                        else entry
                        for entry in statement.entries
                    ]
                }
            )
            report = service.report(period, statement=loss)
            assert report.status is status
            assert report.exit_code == 1
    finally:
        database.dispose()


def test_statement_rejects_duplicate_financial_evidence() -> None:
    entry = StatementEntry(
        component=StatementComponent.SupplierCompute, amount_nanos=1, reference="invoice/line-1"
    )
    with pytest.raises(ValidationError, match="only once"):
        EconomicsStatement(
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            ended_at=datetime(2026, 2, 1, tzinfo=UTC),
            attribution_basis="segment_started_at",
            entries=[entry, entry],
        )
