"""Tests for UsageService database operations."""

from datetime import datetime, timedelta, timezone

from lazycloud_api.database import Database
from lazycloud_api.tests.fixtures.database import (
    make_usage_record,
    make_user,
    make_user_workspace,
    make_workspace,
    requires_db,
)
from shared.models.billing import UsageRecordStatus, UsageRecordType


@requires_db
class TestUsageServiceCRUD:
    """Test basic CRUD operations for UsageService."""

    async def test_create_usage_record(self, db: Database):
        """Test creating a usage record."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        record = make_usage_record(workspace.id, cpu_core_seconds=100.0)
        created = await db.usage.create(record)

        assert created.id is not None
        assert created.workspace_id == workspace.id
        assert created.cpu_core_seconds == 100.0
        assert created.status == UsageRecordStatus.DRAFT

    async def test_create_usage_record_with_memory(self, db: Database):
        """Test creating a usage record with memory usage."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        record = make_usage_record(
            workspace.id,
            cpu_core_seconds=50.0,
            memory_gb_seconds=256.0,
        )
        created = await db.usage.create(record)

        assert created.cpu_core_seconds == 50.0
        assert created.memory_gb_seconds == 256.0

    async def test_get_usage_record_by_id(self, db: Database):
        """Test retrieving a usage record by ID."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        record = make_usage_record(workspace.id)
        created = await db.usage.create(record)

        retrieved = await db.usage.get_by_id(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id

    async def test_update_usage_record(self, db: Database):
        """Test updating a usage record."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        record = make_usage_record(workspace.id, cpu_core_seconds=100.0)
        created = await db.usage.create(record)

        created.cpu_core_seconds = 200.0
        updated = await db.usage.update(created)

        assert updated is not None
        assert updated.cpu_core_seconds == 200.0

    async def test_delete_usage_record(self, db: Database):
        """Test deleting a usage record."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        record = make_usage_record(workspace.id)
        created = await db.usage.create(record)

        await db.usage.delete(created.id)

        retrieved = await db.usage.get_by_id(created.id)
        assert retrieved is None


@requires_db
class TestUsageServiceUpsert:
    """Test upsert methods for UsageService."""

    async def test_upsert_creates_new_record(self, db: Database):
        """Test upsert creates a new record when none exists."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=1)
        end = now

        record = await db.usage.upsert_usage_record(
            workspace_id=workspace.id,
            collection_start=start,
            collection_end=end,
            cpu_core_seconds=100.0,
            memory_gb_seconds=50.0,
            storage_gb_hours=10.0,
        )

        assert record is not None
        assert record.workspace_id == workspace.id
        assert record.cpu_core_seconds == 100.0

    async def test_upsert_updates_existing_record(self, db: Database):
        """Test upsert updates an existing record."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=1)
        end = now

        first = await db.usage.upsert_usage_record(
            workspace_id=workspace.id,
            collection_start=start,
            collection_end=end,
            cpu_core_seconds=100.0,
            memory_gb_seconds=50.0,
            storage_gb_hours=10.0,
        )

        second = await db.usage.upsert_usage_record(
            workspace_id=workspace.id,
            collection_start=start,
            collection_end=end,
            cpu_core_seconds=150.0,
            memory_gb_seconds=75.0,
            storage_gb_hours=15.0,
        )

        assert second.id == first.id
        assert second.cpu_core_seconds == 150.0
        assert second.memory_gb_seconds == 75.0


@requires_db
class TestUsageServiceStatus:
    """Test status update methods."""

    async def test_finalize_record(self, db: Database):
        """Test finalizing a usage record."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        record = make_usage_record(workspace.id)
        created = await db.usage.create(record)

        await db.usage.finalize_record(created.id)

        # Verify status changed by fetching the record
        updated = await db.usage.get_by_id(created.id)
        assert updated is not None
        assert updated.status == UsageRecordStatus.FINALIZED

    async def test_mark_as_reported(self, db: Database):
        """Test marking a record as reported."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        record = make_usage_record(workspace.id)
        created = await db.usage.create(record)

        await db.usage.finalize_record(created.id)
        await db.usage.mark_as_reported(created.id)

        # Verify status changed by fetching the record
        updated = await db.usage.get_by_id(created.id)
        assert updated is not None
        assert updated.status == UsageRecordStatus.REPORTED

    async def test_finalize_already_finalized_record(self, db: Database):
        """Test that finalizing an already finalized record is idempotent."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        record = make_usage_record(workspace.id)
        created = await db.usage.create(record)

        await db.usage.finalize_record(created.id)
        await db.usage.finalize_record(created.id)  # Should not error

        updated = await db.usage.get_by_id(created.id)
        assert updated.status == UsageRecordStatus.FINALIZED


@requires_db
class TestUsageServiceQueries:
    """Test query methods for UsageService."""

    async def test_get_workspace_usage(self, db: Database):
        """Test getting workspace usage in a date range."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=2)
        end = now

        await db.usage.create(
            make_usage_record(
                workspace.id,
                collection_start=start,
                collection_end=start + timedelta(hours=1),
                cpu_core_seconds=100.0,
            )
        )
        await db.usage.create(
            make_usage_record(
                workspace.id,
                collection_start=start + timedelta(hours=1),
                collection_end=end,
                cpu_core_seconds=150.0,
            )
        )

        records = await db.usage.get_workspace_usage(workspace.id, start, end)

        assert len(records) == 2
        total_cpu = sum(r.cpu_core_seconds for r in records)
        assert total_cpu == 250.0

    async def test_get_finalized_usage_records(self, db: Database):
        """Test getting finalized usage records (DAILY type only)."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        # Create DAILY record (the method only returns DAILY records)
        now = datetime.now(timezone.utc)
        record = await db.usage.upsert_usage_record(
            workspace_id=workspace.id,
            collection_start=now - timedelta(days=1),
            collection_end=now,
            cpu_core_seconds=100.0,
            memory_gb_seconds=50.0,
            storage_gb_hours=10.0,
            record_type=UsageRecordType.DAILY,
        )

        await db.usage.finalize_record(record.id)

        finalized = await db.usage.get_finalized_usage()

        assert len(finalized) >= 1
        assert any(r.id == record.id for r in finalized)


@requires_db
class TestUsageServiceMultipleWorkspaces:
    """Test usage tracking across multiple workspaces."""

    async def test_usage_isolated_per_workspace(self, db: Database):
        """Test that usage records are isolated per workspace."""
        user = await db.users.create(make_user())
        ws1 = await db.workspaces.create(make_workspace())
        ws2 = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(make_user_workspace(user.id, ws1.id))
        await db.user_workspaces.create(make_user_workspace(user.id, ws2.id))

        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=1)
        end = now

        await db.usage.create(make_usage_record(ws1.id, cpu_core_seconds=100.0))
        await db.usage.create(make_usage_record(ws2.id, cpu_core_seconds=200.0))

        ws1_records = await db.usage.get_workspace_usage(ws1.id, start, end)
        ws2_records = await db.usage.get_workspace_usage(ws2.id, start, end)

        assert sum(r.cpu_core_seconds for r in ws1_records) == 100.0
        assert sum(r.cpu_core_seconds for r in ws2_records) == 200.0
