"""Billing test fixtures and configuration."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lazycloud_api.database import Database, _create_database
from lazycloud_api.database.session import session_manager
from lazycloud_api.database.usage import UsageRecordPydantic
from lazycloud_api.tests.fixtures.database import (
    make_deployment,
    make_user,
    make_user_workspace,
    make_workspace,
)
from shared.models.billing import (
    UsageCollectionConfig,
    UsageRecordStatus,
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


async def create_interval_records(
    db: Database,
    workspace_id: str,
    date: datetime,
    num_intervals: int = 24,
    cpu_per_interval: float = 3600.0,
    memory_per_interval: float = 3600.0,
    standard_per_interval: float = 0.0,
    shared_per_interval: float = 0.0,
    build_per_interval: float = 0.0,
    endpoint_per_interval: float = 0.0,
) -> list[UsageRecordPydantic]:
    """Create multiple interval records for a day (persisted to DB)."""
    records = []
    day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)

    for i in range(num_intervals):
        start_time = day_start + timedelta(hours=i)
        end_time = start_time + timedelta(hours=1)

        record = await db.usage.upsert_usage_record(
            workspace_id=workspace_id,
            collection_start=start_time,
            collection_end=end_time,
            cpu_core_seconds=cpu_per_interval,
            memory_gb_seconds=memory_per_interval,
            storage_gb_hours=standard_per_interval + shared_per_interval,
            standard_gb_hours=standard_per_interval,
            shared_gb_hours=shared_per_interval,
            build_minutes=build_per_interval,
            public_endpoint_hours=endpoint_per_interval,
            record_type=UsageCollectionConfig.get_record_type(),
            status=UsageRecordStatus.FINALIZED,
        )
        records.append(record)

    return records
