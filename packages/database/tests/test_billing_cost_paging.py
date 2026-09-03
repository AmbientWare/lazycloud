from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from api.server.services import ApiServices
from database.repositories.billing_costs import (
    BillingLedgerCostRepository,
    LedgerCostCursor,
    WorkspaceCostScope,
)
from database.repositories.billing_rates import PlatformRateRepository
from shared.http.usage import UsageCostGroupKey
from shared.timestamps import utc_now
from shared.usage import (
    IMAGE_BUILD_WORKLOAD_ID,
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)

_RATE_AT = timedelta(minutes=1)
_WINDOW_AT = timedelta(minutes=2)
_WINDOW = timedelta(seconds=60)


def test_cost_paging_returns_every_group_once_when_the_deepest_id_is_empty(
    isolated_services: ApiServices,
) -> None:
    """Every priced group is read exactly once, at a level most rows do not reach.

    A container that is not a task carries no `task_id`, so grouping by task puts
    most of a workspace's cost in groups whose deepest id is empty and whose only
    distinguishing value is the workload above it. Two of them here cost the same
    to the nanodollar, which is the pair a cursor keyed on anything less than the
    whole group key drops between pages.
    """

    now = utc_now()
    started_at = now + _WINDOW_AT
    ended_at = started_at + _WINDOW
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
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
        isolated_services.usage.append(
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
    while True:
        with isolated_services.context.database.session() as session:
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

    assert len(read) == len(expected)
    assert dict(read) == expected


def test_app_level_costs_keep_image_builds_apart_from_other_unattributed_usage(
    isolated_services: ApiServices,
) -> None:
    """Image builds reach no app, and still are not the same thing as the rest of
    what reached no app: a build is work someone asked for, a volume byte-second
    is storage sitting there. Grouped by app they come back as two rows."""
    now = utc_now()
    started_at = now + _WINDOW_AT
    ended_at = started_at + _WINDOW
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        PlatformRateRepository(session).publish(
            pricing_version="test.category",
            effective_at=now + _RATE_AT,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )
    for stub_id, quantity in ((IMAGE_BUILD_WORKLOAD_ID, 300), ("", 100)):
        isolated_services.usage.append(
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
    with isolated_services.context.database.session() as session:
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
