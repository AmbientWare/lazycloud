from __future__ import annotations

import os
from datetime import timedelta
from threading import Barrier, Lock, Thread
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from database.repositories.identity import WorkspaceRepository
from database.repositories.observability import UsageRepository
from database.tables.identity import WorkspaceTable
from database.tables.observability import UsageBillingWindowTable, UsageRecordTable
from pydantic import JsonValue
from shared.timestamps import utc_now
from shared.usage import UsageMetric, UsageRecord, UsageUnit
from sqlalchemy import delete, select

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


def test_billing_window_projection_replaces_stable_record_contribution(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    record_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        usage = UsageRepository(session)
        for quantity in (1.0, 2.5):
            usage.record(
                id=record_id,
                workspace_id=workspace_id,
                resource_type="container",
                resource_id="container-idempotent",
                metric=UsageMetric.CpuSeconds,
                quantity=quantity,
                unit=UsageUnit.Seconds,
                metadata={
                    "worker_id": "worker-idempotent",
                    "window_start_ms": 0,
                    "window_end_ms": 1_000,
                },
            )

    overview = isolated_services.usage.billing_overview(
        workspace_id=workspace_id,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        bucket_seconds=3600,
    )
    report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        bucket_seconds=3600,
    )

    assert overview.summary == report.summary
    assert overview.summary[0].quantity == 2.5


def test_billing_ranges_use_metering_time_with_creation_fallback(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    metered_at = now - timedelta(days=2)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        usage = UsageRepository(session)
        usage.append(
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="container",
                resource_id="metered-in-window",
                metric=UsageMetric.CpuSeconds,
                quantity=2,
                unit=UsageUnit.Seconds,
                metadata={"metering_window_started_at": metered_at.isoformat()},
                created_at=now,
            )
        )
        usage.append(
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="container",
                resource_id="created-in-window-only",
                metric=UsageMetric.CpuSeconds,
                quantity=5,
                unit=UsageUnit.Seconds,
                metadata={"metering_window_started_at": now.isoformat()},
                created_at=metered_at,
            )
        )

    start = metered_at - timedelta(hours=1)
    end = metered_at + timedelta(hours=1)
    overview = isolated_services.usage.billing_overview(
        workspace_id=workspace_id,
        start=start,
        end=end,
        bucket_seconds=3_600,
    )
    report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=start,
        end=end,
        bucket_seconds=3_600,
    )

    assert overview.summary == report.summary
    assert overview.summary[0].quantity == 2


def test_billing_projection_recomputes_shared_window_timestamp_after_replacement(
    isolated_services: ApiServices,
) -> None:
    origin = utc_now() - timedelta(days=3)
    cpu_id = str(uuid4())
    memory_id = str(uuid4())
    metadata: dict[str, JsonValue] = {
        "worker_id": "worker-moving-window",
        "window_start_ms": 0,
        "window_end_ms": 1_000,
    }
    labels = {"app_id": "app-moving-window", "stub_id": "stub-moving-window"}
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        usage = UsageRepository(session)
        usage.record(
            id=cpu_id,
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-moving-window",
            metric=UsageMetric.CpuSeconds,
            quantity=1,
            unit=UsageUnit.Seconds,
            labels=labels,
            metadata={**metadata, "metering_window_started_at": origin.isoformat()},
        )
        usage.record(
            id=memory_id,
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-moving-window",
            metric=UsageMetric.MemoryGibSeconds,
            quantity=1,
            unit=UsageUnit.GibSeconds,
            labels=labels,
            metadata={
                **metadata,
                "metering_window_started_at": (origin + timedelta(hours=1)).isoformat(),
            },
        )
        usage.record(
            id=cpu_id,
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-moving-window",
            metric=UsageMetric.CpuSeconds,
            quantity=2,
            unit=UsageUnit.Seconds,
            labels=labels,
            metadata={
                **metadata,
                "metering_window_started_at": (origin + timedelta(hours=2)).isoformat(),
            },
        )

    old_overview = isolated_services.usage.billing_overview(
        workspace_id=workspace_id,
        start=origin - timedelta(minutes=1),
        end=origin + timedelta(minutes=1),
        bucket_seconds=3_600,
    )
    old_report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=origin - timedelta(minutes=1),
        end=origin + timedelta(minutes=1),
        bucket_seconds=3_600,
    )
    moved_overview = isolated_services.usage.billing_overview(
        workspace_id=workspace_id,
        start=origin + timedelta(minutes=59),
        end=origin + timedelta(hours=3),
        bucket_seconds=3_600,
    )
    moved_report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=origin + timedelta(minutes=59),
        end=origin + timedelta(hours=3),
        bucket_seconds=3_600,
    )

    assert old_overview.summary == old_report.summary == ()
    assert moved_overview.summary == moved_report.summary
    assert {line.metric.value: line.quantity for line in moved_overview.summary} == {
        UsageMetric.CpuSeconds.value: 2,
        UsageMetric.MemoryGibSeconds.value: 1,
    }


def test_zero_cost_record_does_not_create_empty_billing_projection(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        UsageRepository(session).record(
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-zero-cost",
            metric=UsageMetric.ContainerCostCents,
            quantity=0,
            unit=UsageUnit.Cents,
            metadata={
                "worker_id": "worker-zero-cost",
                "window_start_ms": 0,
                "window_end_ms": 1_000,
            },
        )
        windows = tuple(
            session.scalars(
                select(UsageBillingWindowTable).where(
                    UsageBillingWindowTable.workspace_id == workspace_id,
                    UsageBillingWindowTable.resource_id == "container-zero-cost",
                )
            )
        )

    assert windows == ()


def test_postgresql_billing_projection_serializes_two_writers() -> None:
    database_url = os.environ.get("LAZYCLOUD_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("LAZYCLOUD_TEST_POSTGRES_URL is required for PostgreSQL concurrency proof")
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=database_url,
            pool_size=4,
            max_overflow=0,
            application_name=DatabaseApplicationName.Test,
        )
    )
    workspace_name = f"usage-concurrency-{uuid4()}"
    with database.session() as session:
        workspace = WorkspaceRepository(session).create(name=workspace_name)

    failures: list[BaseException] = []
    failures_lock = Lock()
    try:
        for iteration in range(20):
            record_id = str(uuid4())
            barrier = Barrier(2)

            def write(
                quantity: float,
                current_barrier: Barrier,
                current_record_id: str,
                current_iteration: int,
            ) -> None:
                try:
                    current_barrier.wait(timeout=10)
                    with database.session() as session:
                        UsageRepository(session).record(
                            id=current_record_id,
                            workspace_id=workspace.id,
                            resource_type="container",
                            resource_id=f"container-{current_iteration}",
                            metric=UsageMetric.CpuSeconds,
                            quantity=quantity,
                            unit=UsageUnit.Seconds,
                            metadata={
                                "worker_id": "worker-concurrent",
                                "window_start_ms": current_iteration * 1_000,
                                "window_end_ms": (current_iteration + 1) * 1_000,
                            },
                        )
                except BaseException as error:
                    with failures_lock:
                        failures.append(error)

            writers = (
                Thread(target=write, args=(1.0, barrier, record_id, iteration)),
                Thread(target=write, args=(2.5, barrier, record_id, iteration)),
            )
            for writer in writers:
                writer.start()
            for writer in writers:
                writer.join(timeout=15)
                assert not writer.is_alive()
            assert not failures

            with database.session() as session:
                raw = session.scalar(
                    select(UsageRecordTable).where(
                        UsageRecordTable.workspace_id == workspace.id,
                        UsageRecordTable.id == record_id,
                    )
                )
                windows = tuple(
                    session.scalars(
                        select(UsageBillingWindowTable).where(
                            UsageBillingWindowTable.workspace_id == workspace.id,
                            UsageBillingWindowTable.resource_id == f"container-{iteration}",
                        )
                    )
                )
                assert raw is not None
                assert len(windows) == 1
                assert windows[0].cpu_direct_records == 1
                quantity = raw.payload["quantity"]
                assert isinstance(quantity, (int, float)) and not isinstance(quantity, bool)
                assert abs(windows[0].cpu_direct_seconds - float(quantity)) < 1e-9
    finally:
        with database.session() as session:
            session.execute(delete(WorkspaceTable).where(WorkspaceTable.id == workspace.id))
        database.dispose()


def test_postgresql_billing_projection_serializes_shared_window_writers() -> None:
    database_url = os.environ.get("LAZYCLOUD_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("LAZYCLOUD_TEST_POSTGRES_URL is required for PostgreSQL concurrency proof")
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=database_url,
            pool_size=4,
            max_overflow=0,
            application_name=DatabaseApplicationName.Test,
        )
    )
    workspace_name = f"usage-window-concurrency-{uuid4()}"
    with database.session() as session:
        workspace = WorkspaceRepository(session).create(name=workspace_name)

    failures: list[BaseException] = []
    failures_lock = Lock()
    try:
        for iteration in range(20):
            barrier = Barrier(2)

            def write(
                metric: UsageMetric,
                quantity: float,
                current_record_id: str,
                current_barrier: Barrier,
                current_iteration: int,
            ) -> None:
                try:
                    current_barrier.wait(timeout=10)
                    with database.session() as session:
                        UsageRepository(session).record(
                            id=current_record_id,
                            workspace_id=workspace.id,
                            resource_type="container",
                            resource_id=f"shared-container-{current_iteration}",
                            metric=metric,
                            quantity=quantity,
                            unit=UsageUnit.Seconds,
                            metadata={
                                "worker_id": "shared-worker",
                                "window_start_ms": current_iteration * 1_000,
                                "window_end_ms": (current_iteration + 1) * 1_000,
                            },
                        )
                except BaseException as error:
                    with failures_lock:
                        failures.append(error)

            writers = (
                Thread(
                    target=write,
                    args=(UsageMetric.CpuSeconds, 1.0, str(uuid4()), barrier, iteration),
                ),
                Thread(
                    target=write,
                    args=(UsageMetric.CpuSeconds, 2.5, str(uuid4()), barrier, iteration),
                ),
            )
            for writer in writers:
                writer.start()
            for writer in writers:
                writer.join(timeout=15)
                assert not writer.is_alive()
            assert not failures

            with database.session() as session:
                window = session.scalar(
                    select(UsageBillingWindowTable).where(
                        UsageBillingWindowTable.workspace_id == workspace.id,
                        UsageBillingWindowTable.resource_id == f"shared-container-{iteration}",
                    )
                )
                assert window is not None
                assert window.cpu_direct_records == 2
                assert abs(window.cpu_direct_seconds - 3.5) < 1e-9
    finally:
        with database.session() as session:
            session.execute(delete(WorkspaceTable).where(WorkspaceTable.id == workspace.id))
        database.dispose()
