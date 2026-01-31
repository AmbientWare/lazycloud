"""Billing test fixtures and configuration."""

from datetime import date, datetime

import pytest
from backend.database import Database, _create_database
from backend.database.models import BreakdownType, DailyUsageRecordPydantic
from backend.database.session import session_manager
from models.storage import STORAGE_CLASS_STANDARD
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fixtures.database import (
    make_deployment,
    make_user,
    make_user_workspace,
    make_workspace,
)


@pytest.fixture
async def billing_db_session() -> AsyncSession:
    """Provide a transactional session that rolls back after each test."""
    await session_manager.reset()

    session_manager._ensure_initialized()
    engine = session_manager.engine

    conn = await engine.connect()
    try:
        trans = await conn.begin()
        try:
            session = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                yield session
            finally:
                await session.close()
        finally:
            await trans.rollback()
    finally:
        await conn.close()
        await session_manager.reset()


@pytest.fixture
def billing_db(billing_db_session: AsyncSession) -> Database:
    """Get Database instance bound to the test session."""
    return _create_database(billing_db_session)


@pytest.fixture
async def billing_workspace(billing_db: Database):
    """Create a test workspace for billing tests."""
    user = await billing_db.users.create(make_user())
    workspace = await billing_db.workspaces.create(make_workspace())
    await billing_db.user_workspaces.create(make_user_workspace(user.id, workspace.id))
    return workspace


@pytest.fixture
async def billing_deployment(billing_db: Database, billing_workspace):
    """Create a test deployment for billing tests."""
    deployment = await billing_db.compose_deployments.create(
        make_deployment(billing_workspace.id)
    )
    return deployment


async def create_daily_record(
    db: Database,
    workspace_id: str,
    usage_date: date,
    cpu_core_seconds: float = 3600.0,
    memory_gb_seconds: float = 3600.0,
    storage_gb_months: float = 0.0,
    build_minutes: float = 0.0,
    intervals_collected: int = 1,
    billing_attempts: int = 0,
) -> DailyUsageRecordPydantic:
    """Create a daily usage record for testing with specified values."""
    record = await db.usage.get_or_create_daily_record(
        workspace_id=workspace_id,
        usage_date=usage_date,
        expected_intervals=96,
    )

    # Increment usage - this adds 1 to intervals_collected
    await db.usage.atomic_increment_usage(
        record_id=record.id,
        cpu_core_seconds=cpu_core_seconds,
        memory_gb_seconds=memory_gb_seconds,
        storage_gb_months=storage_gb_months,
        build_minutes=build_minutes,
    )

    # If caller wants more intervals, add zero-value increments
    for _ in range(intervals_collected - 1):
        await db.usage.atomic_increment_usage(
            record_id=record.id,
            cpu_core_seconds=0.0,
            memory_gb_seconds=0.0,
            build_minutes=0.0,
        )

    # Set billing attempts if specified
    for _ in range(billing_attempts):
        await db.usage.increment_billing_attempt(record.id)

    # Re-fetch to get updated values
    records = await db.usage.get_workspace_daily_usage(
        workspace_id=workspace_id,
        start_date=usage_date,
        end_date=usage_date,
    )
    return records[0] if records else record


async def create_breakdown_events(
    db: Database,
    workspace_id: str,
    interval_start: datetime,
    interval_end: datetime,
    deployment_id: str | None = None,
    num_pods: int = 2,
    num_pvcs: int = 1,
    cpu_per_pod: float = 100.0,
    memory_per_pod: float = 200.0,
    gb_hours_per_pvc: float = 10.0,
) -> None:
    """Create breakdown events for testing dashboards."""
    for i in range(num_pods):
        await db.usage.add_breakdown_event(
            workspace_id=workspace_id,
            interval_start=interval_start,
            interval_end=interval_end,
            breakdown_type=BreakdownType.COMPUTE,
            resource_name=f"pod-{i}",
            deployment_id=deployment_id,
            service_name=f"service-{i}",
            cpu_core_seconds=cpu_per_pod,
            memory_gb_seconds=memory_per_pod,
        )

    for i in range(num_pvcs):
        await db.usage.add_breakdown_event(
            workspace_id=workspace_id,
            interval_start=interval_start,
            interval_end=interval_end,
            breakdown_type=BreakdownType.STORAGE,
            resource_name=f"pvc-{i}",
            deployment_id=deployment_id,
            storage_class=STORAGE_CLASS_STANDARD,
            gb_hours=gb_hours_per_pvc,
        )
