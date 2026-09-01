from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from database.repositories.execution import LogRepository, TaskRepository
from database.repositories.identity import WorkspaceRepository
from shared.logs import LogEntry
from shared.realtime.streams import LogStreamQuery
from shared.tasks import Task
from sqlalchemy.engine import URL

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
    bootstrap_database,
)


def test_log_pages_are_workspace_scoped_and_resume_by_row_identity(
    postgres_database_url: URL,
) -> None:
    database_url = postgres_database_url.render_as_string(hide_password=False)
    bootstrap_database(database_url)
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
