from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from database.context import ServiceContext
from database.records.apps import StubRecord
from database.repositories.apps import StubRepository
from database.repositories.billing_costs import (
    BillingLedgerCostRepository,
    LedgerCostCursor,
    PayerCostScope,
    WorkspaceCostScope,
)
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.billing_rates import PlatformRateRepository
from database.repositories.observability import UsageRepository
from observability.usage import UsageService
from shared.artifacts import ARTIFACT_STORAGE_SUBJECT
from shared.billing_credits import CreditGrant, CreditKind
from shared.billing_rate_card import PUBLISHED_METERED_RATE_HISTORY
from shared.http.usage import UsageCostCategory, UsageCostGroupKey
from shared.timestamps import utc_now
from shared.usage import (
    IMAGE_BUILD_WORKLOAD_ID,
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from tests.workspaces import unfunded_billing_account

_RATE_AT = timedelta(minutes=1)
_WINDOW_AT = timedelta(minutes=2)
_WINDOW = timedelta(seconds=60)


def test_cost_breakdown_resolves_workloads_without_looking_up_billing_categories(
    service_context: ServiceContext,
) -> None:
    now = max(utc_now(), *(card.effective_at for card in PUBLISHED_METERED_RATE_HISTORY))
    started_at = now + _WINDOW_AT
    ended_at = started_at + _WINDOW
    with service_context.database.session() as session:
        workspace_id = service_context.default_workspace_id(session)
        workload = StubRepository(session).upsert(
            StubRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                name="training",
            )
        )
        PlatformRateRepository(session).publish(
            pricing_version="test.cost-categories",
            effective_at=now + _RATE_AT,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(1),
        )
    for workload_id, metric, unit, quantity in (
        (
            ARTIFACT_STORAGE_SUBJECT,
            UsageMetric.ArtifactStorageByteSeconds,
            UsageUnit.ByteSeconds,
            300,
        ),
        (IMAGE_BUILD_WORKLOAD_ID, UsageMetric.NetworkEgressBytes, UsageUnit.Bytes, 200),
        (workload.id, UsageMetric.NetworkEgressBytes, UsageUnit.Bytes, 100),
    ):
        UsageService(service_context).append(
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="workspace",
                resource_id=workspace_id,
                metric=metric,
                quantity=quantity,
                unit=unit,
                labels={"stub_id": workload_id},
                metadata={
                    METERING_WINDOW_STARTED_AT_METADATA_KEY: started_at.isoformat(),
                    METERING_WINDOW_ENDED_AT_METADATA_KEY: ended_at.isoformat(),
                },
            )
        )
    with service_context.database.session() as session:
        page = BillingLedgerCostRepository(session).page(
            scope=WorkspaceCostScope((workspace_id,)),
            start=started_at,
            end=ended_at + _WINDOW,
            group_by=UsageCostGroupKey.Workload,
            limit=10,
        )
    assert [
        (row.workload_id, row.workload_name, row.category, row.cost_nanos) for row in page.rows
    ] == [
        (ARTIFACT_STORAGE_SUBJECT, "Artifacts", "", 300),
        (IMAGE_BUILD_WORKLOAD_ID, "", IMAGE_BUILD_WORKLOAD_ID, 200),
        (workload.id, "training", "", 100),
    ]


def test_cost_paging_returns_every_group_once_when_the_deepest_id_is_empty(
    service_context: ServiceContext,
) -> None:
    """Equal costs and empty task IDs still require the whole group key in the cursor."""

    now = max(utc_now(), *(card.effective_at for card in PUBLISHED_METERED_RATE_HISTORY))
    started_at = now + _WINDOW_AT
    ended_at = started_at + _WINDOW
    with service_context.database.session() as session:
        workspace_id = service_context.default_workspace_id(session)
        PlatformRateRepository(session).publish(
            pricing_version="test.paging",
            effective_at=now + _RATE_AT,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )

    app_id = str(uuid4())
    expected: dict[tuple[str, str, str], int] = {}
    for cost_nanos in (300, 200, 200, 100, 100):
        workload_id = str(uuid4())
        expected[(app_id, workload_id, "")] = cost_nanos
        UsageService(service_context).append(
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="workspace",
                resource_id=workspace_id,
                metric=UsageMetric.NetworkEgressBytes,
                quantity=cost_nanos,
                unit=UsageUnit.Bytes,
                labels={"app_id": app_id, "stub_id": workload_id},
                metadata={
                    METERING_WINDOW_STARTED_AT_METADATA_KEY: started_at.isoformat(),
                    METERING_WINDOW_ENDED_AT_METADATA_KEY: ended_at.isoformat(),
                },
            )
        )

    read: list[tuple[tuple[str, str, str], int]] = []
    cursor: LedgerCostCursor | None = None
    for _ in range(len(expected) + 1):
        with service_context.database.session() as session:
            page = BillingLedgerCostRepository(session).page(
                scope=WorkspaceCostScope((workspace_id,)),
                start=started_at - _WINDOW,
                end=ended_at + _WINDOW,
                group_by=UsageCostGroupKey.Task,
                limit=2,
                cursor=cursor,
            )
        read.extend(
            ((row.app_id, row.workload_id, row.task_id), row.cost_nanos) for row in page.rows
        )
        cursor = page.next
        if cursor is None:
            break

    assert cursor is None
    assert len(read) == len(expected)
    assert dict(read) == expected


def test_app_level_costs_keep_image_builds_apart_from_other_unattributed_usage(
    service_context: ServiceContext,
) -> None:
    now = max(utc_now(), *(card.effective_at for card in PUBLISHED_METERED_RATE_HISTORY))
    started_at = now + _WINDOW_AT
    ended_at = started_at + _WINDOW
    with service_context.database.session() as session:
        workspace_id = service_context.default_workspace_id(session)
        PlatformRateRepository(session).publish(
            pricing_version="test.category",
            effective_at=now + _RATE_AT,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )
    for stub_id, quantity in ((IMAGE_BUILD_WORKLOAD_ID, 300), ("", 100)):
        UsageService(service_context).append(
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="container",
                resource_id=str(uuid4()),
                metric=UsageMetric.NetworkEgressBytes,
                quantity=quantity,
                unit=UsageUnit.Bytes,
                labels={"stub_id": stub_id},
                metadata={
                    METERING_WINDOW_STARTED_AT_METADATA_KEY: started_at.isoformat(),
                    METERING_WINDOW_ENDED_AT_METADATA_KEY: ended_at.isoformat(),
                },
            )
        )
    with service_context.database.session() as session:
        page = BillingLedgerCostRepository(session).page(
            scope=WorkspaceCostScope(workspace_ids=(workspace_id,)),
            start=started_at,
            end=ended_at + _WINDOW,
            group_by=UsageCostGroupKey.App,
            limit=10,
        )

    assert [(row.app_id, row.category, row.cost_nanos) for row in page.rows] == [
        ("", IMAGE_BUILD_WORKLOAD_ID, 300),
        ("", "", 100),
    ]
    with service_context.database.session() as session:
        repository = BillingLedgerCostRepository(session)
        scope = WorkspaceCostScope(workspace_ids=(workspace_id,))
        runs = repository.page(
            scope=scope,
            start=started_at,
            end=ended_at + _WINDOW,
            group_by=UsageCostGroupKey.Task,
            workspace_id=workspace_id,
            category=UsageCostCategory.Unattributed,
            limit=10,
        )
        total = repository.window_cost_nanos(
            scope=scope,
            start=started_at,
            end=ended_at + _WINDOW,
            workspace_id=workspace_id,
            category=UsageCostCategory.Unattributed,
        )
    assert [(row.workload_id, row.cost_nanos) for row in runs.rows] == [("", 100)]
    assert total == 100


def test_subscription_coverage_counts_allocations_for_the_payer_and_usage_window(
    service_context: ServiceContext,
) -> None:
    now = max(utc_now(), *(card.effective_at for card in PUBLISHED_METERED_RATE_HISTORY))
    start = now + _WINDOW_AT
    end = start + _WINDOW
    accounts = [
        unfunded_billing_account(
            service_context, period_started_at=now, period_ended_at=end + timedelta(days=1)
        )
        for _ in range(2)
    ]
    with service_context.database.session() as session:
        PlatformRateRepository(session).publish(
            pricing_version="test.subscription-coverage",
            effective_at=now + _RATE_AT,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )
        credits = BillingCreditRepository(session)
        for user_id, workspace_id in accounts:
            credits.issue(
                user_id=user_id,
                grant=CreditGrant(str(uuid4()), CreditKind.Subscription, 60, now, end),
            )
            credits.issue(
                user_id=user_id,
                grant=CreditGrant(str(uuid4()), CreditKind.Purchased, 100, now, None),
            )
            for window_start, quantity in ((start, 100), (end, 20)):
                record = UsageRecord(
                    id=str(uuid4()),
                    workspace_id=workspace_id,
                    resource_type="workspace",
                    resource_id=workspace_id,
                    metric=UsageMetric.NetworkEgressBytes,
                    quantity=quantity,
                    unit=UsageUnit.Bytes,
                    metadata={
                        METERING_WINDOW_STARTED_AT_METADATA_KEY: window_start.isoformat(),
                        METERING_WINDOW_ENDED_AT_METADATA_KEY: (window_start + _WINDOW).isoformat(),
                    },
                )
                UsageRepository(session).append(record)
                BillingLedgerRepository(session).price_record(record)
            credits.issue(
                user_id=user_id,
                grant=CreditGrant(str(uuid4()), CreditKind.Subscription, 500, now, end),
            )
        repository = BillingLedgerCostRepository(session)
        user_id, workspace_id = accounts[0]
        scope = PayerCostScope(owner_user_id=user_id)
        assert repository.subscription_credit_nanos(scope=scope, start=start, end=end) == 60
        assert repository.subscription_credit_nanos(scope=scope, start=end, end=end + _WINDOW) == 0
        assert repository.window_cost_nanos(scope=scope, start=start, end=end) == 100
        assert (
            repository.window_cost_nanos(
                scope=scope, start=start, end=end, workspace_id=accounts[1][1]
            )
            == 0
        )
