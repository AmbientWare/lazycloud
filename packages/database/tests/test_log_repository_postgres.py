from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from database.context import ServiceContext
from database.repositories.billing import BillingAccountRepository
from database.repositories.execution import LogRepository, TaskRepository
from database.repositories.identity import (
    UserRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from database.tables.execution import LogTable
from observability.log_retention import LogRetentionService
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import published_plan
from shared.logs import LogEntry
from shared.realtime.streams import LogStreamQuery
from shared.tasks import Task
from shared.timestamps import utc_now
from sqlalchemy import select, update
from sqlalchemy.engine import URL

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
)


def test_log_pages_are_workspace_scoped_and_resume_by_row_identity(
    migrated_database_url: URL,
) -> None:
    database_url = migrated_database_url.render_as_string(hide_password=False)
    database = DatabaseClient.from_settings(
        DatabaseSettings(url=database_url, application_name=DatabaseApplicationName.Test)
    )
    try:
        with database.session() as session:
            first_workspace = WorkspaceRepository(session).create(name="first")
            second_workspace = WorkspaceRepository(session).create(name="second")
            first_task = TaskRepository(session).upsert(
                Task(id=str(uuid4()), name="first-task", workspace_id=first_workspace.id),
                workspace_id=first_workspace.id,
            )
            second_task = TaskRepository(session).upsert(
                Task(id=str(uuid4()), name="second-task", workspace_id=second_workspace.id),
                workspace_id=second_workspace.id,
            )
            started_at = datetime(2026, 8, 31, tzinfo=UTC)
            logs = LogRepository(session)
            for offset, message in enumerate(("first", "second", "third")):
                logs.append(
                    LogEntry(
                        id=str(uuid4()),
                        task_id=first_task.id,
                        message=message,
                        created_at=started_at + timedelta(seconds=offset),
                    ),
                    workspace_id=first_workspace.id,
                )
            logs.append(
                LogEntry(
                    id=str(uuid4()),
                    task_id=second_task.id,
                    message="other workspace",
                    created_at=started_at + timedelta(seconds=4),
                ),
                workspace_id=second_workspace.id,
            )

        with database.session() as session:
            repository = LogRepository(session)
            query = LogStreamQuery(workspace_id=first_workspace.id)
            newest = repository.page(
                query,
                workspace_id=first_workspace.id,
                limit=2,
            )
            older = repository.page(
                query,
                workspace_id=first_workspace.id,
                limit=2,
                cursor=newest.next,
            )
            followed = repository.page_after(
                query,
                workspace_id=first_workspace.id,
                limit=10,
                cursor=older.data[0].cursor,
            )

        assert [item.entry.message for item in newest.data] == ["second", "third"]
        assert newest.next is not None
        assert [item.entry.message for item in older.data] == ["first"]
        assert older.next is None
        assert [item.entry.message for item in followed.data] == ["second", "third"]
        assert all(item.workspace_id == first_workspace.id for item in newest.data + older.data)
    finally:
        database.dispose()


def test_log_retention_deletes_only_expired_rows_under_each_owners_plan(
    migrated_database_url: URL,
    tmp_path: Path,
) -> None:
    url = migrated_database_url.render_as_string(hide_password=False)
    database = DatabaseClient.from_settings(
        DatabaseSettings(url=url, application_name=DatabaseApplicationName.Test)
    )
    now = utc_now()
    service = LogRetentionService(
        ServiceContext.create(database, root=tmp_path, create_schema=False)
    )
    try:
        workspaces: dict[BillingPlanId, str] = {}
        with database.session() as session:
            for plan, days in ((BillingPlanId.Free, 1), (BillingPlanId.Team, 30)):
                owner = UserRepository(session).create(display_name=plan.value)
                workspace = WorkspaceRepository(session).create(name=plan.value)
                workspaces[plan] = workspace.id
                WorkspaceMemberRepository(session).ensure_owner(
                    workspace_id=workspace.id, user_id=owner.id
                )
                BillingAccountRepository(session).upsert(
                    user_id=owner.id,
                    status=BillingAccountStatus.Active,
                    provider_customer_id=f"customer-{owner.id}",
                    provider_subscription_id=f"subscription-{owner.id}",
                    plan=plan,
                    subscription_terms_version=published_plan(plan).terms_version,
                    scheduled_terms_version=None,
                    scheduled_change_at=None,
                )
                task = TaskRepository(session).upsert(
                    Task(id=str(uuid4()), name=plan.value, workspace_id=workspace.id),
                    workspace_id=workspace.id,
                )
                cutoff = now - timedelta(days=days)
                for message, timestamp in (
                    ("expired", cutoff - timedelta(microseconds=1)),
                    ("boundary", cutoff),
                    ("recent", now),
                ):
                    entry = LogRepository(session).append(
                        LogEntry(
                            id=str(uuid4()), task_id=task.id, message=message, created_at=timestamp
                        ),
                        workspace_id=workspace.id,
                    )
                    session.execute(
                        update(LogTable).where(LogTable.id == entry.id).values(created_at=timestamp)
                    )

        for plan, workspace_id in workspaces.items():
            days = 1 if plan is BillingPlanId.Free else 30
            assert service.cutoff(workspace_id, now=now) == now - timedelta(days=days)
        assert service.prune(now=now, limit=1) == 1
        assert service.prune(now=now, limit=1) == 1
        assert service.prune(now=now, limit=1) == 0
        with database.session() as session:
            remaining = list(session.execute(select(LogTable.workspace_id, LogTable.message)))
        assert sorted(remaining) == sorted(
            (workspace, message)
            for workspace in workspaces.values()
            for message in ("boundary", "recent")
        )
    finally:
        database.dispose()
