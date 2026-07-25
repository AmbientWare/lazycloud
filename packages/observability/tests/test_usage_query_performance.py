from __future__ import annotations

import os
from collections.abc import Callable
from datetime import timedelta
from threading import Barrier, Lock, Thread
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from database.records.apps import AppRecord, StubRecord
from database.repositories.apps import AppRepository, StubRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.observability import UsageRepository
from database.tables.identity import WorkspaceTable
from database.tables.observability import UsageBillingWindowTable, UsageRecordTable
from pydantic import JsonValue
from shared.deployments import StubKind
from shared.timestamps import utc_now
from shared.usage import UsageGroupKey, UsageMetric, UsageRecord, UsageUnit
from shared.usage_query import UsageQuery
from sqlalchemy import delete, event, select
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.engine.interfaces import DBAPICursor, _DBAPIAnyExecuteParams

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


def _statement_collector(
    statements: list[str],
) -> Callable[
    [Connection, DBAPICursor, str, _DBAPIAnyExecuteParams, ExecutionContext | None, bool],
    None,
]:
    def collect(
        _connection: Connection,
        _cursor: DBAPICursor,
        statement: str,
        _parameters: _DBAPIAnyExecuteParams,
        _context: ExecutionContext | None,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    return collect


def test_billing_report_projects_only_billable_evidence_with_constant_query_count(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    app_id = str(uuid4())
    stub_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        AppRepository(session).upsert(
            AppRecord(id=app_id, workspace_id=workspace_id, name="projection-app")
        )
        StubRepository(session).upsert(
            StubRecord(
                id=stub_id,
                workspace_id=workspace_id,
                app_id=app_id,
                name="projection-workload",
                kind=StubKind.Function,
            )
        )
        usage = UsageRepository(session)
        labels = {
            "app_id": app_id,
            "stub_id": stub_id,
            "cpu_millicores": "1000",
            "mem_mb": "1024",
            "gpu_count": "0",
        }
        for index in range(25):
            resource_id = f"container-{index}"
            usage.record(
                workspace_id=workspace_id,
                resource_type="container",
                resource_id=resource_id,
                metric=UsageMetric.ContainerDurationMilliseconds,
                quantity=1_000,
                unit=UsageUnit.Milliseconds,
                labels=labels,
            )
            usage.record(
                workspace_id=workspace_id,
                resource_type="container",
                resource_id=resource_id,
                metric=UsageMetric.NetworkIngressBytes,
                quantity=10_000,
                unit=UsageUnit.Bytes,
                labels=labels,
            )

    statements: list[str] = []
    event.listen(
        isolated_services.context.database.engine,
        "before_cursor_execute",
        _statement_collector(statements),
    )

    report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        bucket_seconds=3600,
    )

    selects = [statement for statement in statements if statement.lstrip().startswith("SELECT")]
    assert len(selects) == 3
    usage_statement = next(statement for statement in selects if "usage_records" in statement)
    assert "usage_records.metric IN" in usage_statement
    assert "network_ingress_bytes" not in usage_statement
    assert "json_extract" in usage_statement.lower()
    assert report.summary[0].quantity == 25
    assert report.workloads[0].workload_id == stub_id

    statement_count = len(statements)
    overview = isolated_services.usage.billing_overview(
        workspace_id=workspace_id,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        bucket_seconds=3600,
    )
    overview_selects = [
        statement
        for statement in statements[statement_count:]
        if statement.lstrip().startswith(("SELECT", "WITH"))
    ]
    assert len(overview_selects) == 2
    aggregate_statement = next(
        statement for statement in overview_selects if "usage_billing_windows" in statement
    )
    assert "GROUP BY" in aggregate_statement
    assert overview.summary == report.summary
    assert [
        (row.app_id, row.app_name, row.tasks, row.total_cost_nanos, row.lines)
        for row in overview.apps
    ] == [
        (row.app_id, row.app_name, row.tasks, row.total_cost_nanos, row.lines)
        for row in report.apps
    ]
    assert overview.activity == report.activity

    statement_count = len(statements)
    workloads = isolated_services.usage.billing_workloads(
        workspace_id=workspace_id,
        app_id=app_id,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        bucket_seconds=3600,
    )
    detail_selects = [
        statement
        for statement in statements[statement_count:]
        if statement.lstrip().startswith(("SELECT", "WITH"))
    ]
    assert len(detail_selects) == 3
    assert "app_id" in next(
        statement for statement in detail_selects if "usage_billing_windows" in statement
    )
    assert workloads.data == report.workloads


def test_usage_summary_groups_and_filters_in_one_sql_statement(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        usage = UsageRepository(session)
        for app_id, quantity in (("app-a", 2), ("app-a", 3), ("app-b", 5)):
            usage.record(
                workspace_id=workspace_id,
                resource_type="function",
                resource_id=f"resource-{app_id}-{quantity}",
                metric=UsageMetric.TaskCount,
                quantity=quantity,
                unit=UsageUnit.Count,
                labels={"app_id": app_id, "kind": "function"},
            )

    statements: list[str] = []
    event.listen(
        isolated_services.context.database.engine,
        "before_cursor_execute",
        _statement_collector(statements),
    )

    rows = isolated_services.usage.aggregate(
        query=UsageQuery(
            workspace_id=workspace_id,
            created_after=now - timedelta(hours=1),
            labels={"kind": "function"},
        ),
        metric=UsageMetric.TaskCount,
        group_by=(UsageGroupKey.App,),
    )

    selects = [statement for statement in statements if statement.lstrip().startswith("SELECT")]
    assert len(selects) == 1
    assert "sum(usage_records.quantity)" in selects[0]
    assert {row.labels["app_id"]: row.quantity for row in rows} == {
        "app-a": 5,
        "app-b": 5,
    }


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
