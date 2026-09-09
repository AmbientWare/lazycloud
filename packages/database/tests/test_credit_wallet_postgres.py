from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_ledger import (
    BillingLedgerRepository,
    ContainerBillingShapeRepository,
)
from database.repositories.billing_rates import ComputeRateRepository, PlatformRateRepository
from database.repositories.identity import (
    UserRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from database.repositories.observability import UsageRepository
from database.tables.billing_outbox import BillingMeterOutboxTable
from shared.billing_accounts import BillingAccountStatus
from shared.billing_credits import CreditGrant, CreditKind
from shared.billing_quotes import ContainerShape
from shared.timestamps import utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageBillingOwner,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from sqlalchemy import func, select
from sqlalchemy.engine import URL

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings, bootstrap_database


def test_wallet_debits_without_a_subscription_and_never_rebills_expired_debt(
    postgres_database_url: URL,
) -> None:
    url = postgres_database_url.render_as_string(hide_password=False)
    bootstrap_database(url)
    database = DatabaseClient.from_settings(
        DatabaseSettings(url=url, application_name=DatabaseApplicationName.Test)
    )
    now = utc_now()
    start = now - timedelta(minutes=1)
    end = start + timedelta(seconds=1)
    expires = now + timedelta(days=1)
    container_id = str(uuid4())
    try:
        with database.session() as session:
            user_id = UserRepository(session).create(display_name="wallet owner").id
            workspace_id = WorkspaceRepository(session).create(name="wallet usage").id
            WorkspaceMemberRepository(session).ensure_owner(
                workspace_id=workspace_id, user_id=user_id
            )
            BillingAccountRepository(session).upsert(
                user_id=user_id,
                status=BillingAccountStatus.Active,
                provider_customer_id=f"cus_{uuid4()}",
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
            credits.issue(
                user_id=user_id,
                grant=CreditGrant("initial-payment", CreditKind.Purchased, 3, start),
            )
            shape = ContainerShape(UsageBillingOwner.PlatformFleet, "a100", 1000, 1024, 1)
            ContainerBillingShapeRepository(session).record(
                container_id=container_id, workspace_id=workspace_id, shape=shape
            )
            ComputeRateRepository(session).publish(
                billing_owner=shape.billing_owner,
                gpu_type=shape.gpu_type,
                pricing_version="wallet-proof",
                effective_at=start,
                nanos_per_container_second=Decimal(0),
                nanos_per_cpu_core_second=Decimal(1),
                nanos_per_memory_gib_second=Decimal(1),
                nanos_per_gpu_card_second=Decimal(1),
            )
            PlatformRateRepository(session).publish(
                pricing_version="wallet-proof",
                effective_at=start,
                nanos_per_egress_byte=Decimal(1),
                nanos_per_volume_byte_second=Decimal(1),
            )
        records = [
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="container"
                if metric is UsageMetric.ContainerDurationMilliseconds
                else "workspace",
                resource_id=container_id
                if metric is UsageMetric.ContainerDurationMilliseconds
                else workspace_id,
                metric=metric,
                quantity=quantity,
                unit=unit,
                labels={"cpu_millicores": "1000", "mem_mb": "1024", "gpu_count": "1"},
                metadata={
                    METERING_WINDOW_STARTED_AT_METADATA_KEY: start.isoformat(),
                    METERING_WINDOW_ENDED_AT_METADATA_KEY: end.isoformat(),
                },
            )
            for metric, quantity, unit in (
                (UsageMetric.ContainerDurationMilliseconds, 1000, UsageUnit.Milliseconds),
                (UsageMetric.NetworkEgressBytes, 1, UsageUnit.Bytes),
                (UsageMetric.PersistentVolumeByteSeconds, 1, UsageUnit.ByteSeconds),
            )
        ]
        with database.session() as session:
            for record in records:
                UsageRepository(session).append(record)

        def record_usage(record: UsageRecord) -> None:
            with database.session() as session:
                BillingLedgerRepository(session).price_record(record)

        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(record_usage, records * 2))
        with database.session() as session:
            credits = BillingCreditRepository(session)
            assert credits.balance(user_id=user_id, at=now) == -2
            adjustments = credits.account_adjustments(user_id=user_id, start=start, end=end)
            assert sum(value.credited_nanos for value in adjustments.values()) == 3
            assert sum(value.unpaid_nanos for value in adjustments.values()) == 2
            assert session.scalar(select(func.count()).select_from(BillingMeterOutboxTable)) == 0

        purchase = CreditGrant("payment", CreditKind.Purchased, 4, now)

        def receive_payment() -> str:
            with database.session() as session:
                return BillingCreditRepository(session).issue(user_id=user_id, grant=purchase)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(receive_payment) for _ in range(3)]
            lot_ids = [future.result() for future in futures]
        assert len(set(lot_ids)) == 1
        with database.session() as session:
            credits = BillingCreditRepository(session)
            assert credits.balance(user_id=user_id, at=now) == 2
            credits.adjust(
                user_id=user_id,
                credit_lot_id=lot_ids[0],
                source_id="refund",
                amount_nanos=-4,
                effective_at=now,
            )
            assert credits.balance(user_id=user_id, at=now) == -2
            subscription = CreditGrant("renewal", CreditKind.Subscription, 3, now, expires)
            credits.issue(user_id=user_id, grant=subscription)
            assert credits.balance(user_id=user_id, at=now) == 1
        with database.session() as session:
            credits = BillingCreditRepository(session)
            assert credits.balance(user_id=user_id, at=expires) == 0
            credits.issue(user_id=user_id, grant=subscription)
            assert credits.balance(user_id=user_id, at=expires) == 0
            credits.adjust(
                user_id=user_id,
                credit_lot_id=lot_ids[0],
                source_id="refund-reversed",
                amount_nanos=4,
                effective_at=expires,
            )
            assert credits.balance(user_id=user_id, at=expires) == 4
    finally:
        database.dispose()
