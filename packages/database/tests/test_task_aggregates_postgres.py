from __future__ import annotations

from uuid import uuid4

from database.repositories.execution import TaskRepository
from database.repositories.identity import WorkspaceRepository
from shared.tasks import Task, TaskStatus
from sqlalchemy.engine import URL

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
)


def test_task_aggregates_run_against_postgresql_column_types(
    migrated_database_url: URL,
) -> None:
    """The aggregates have to survive the types the real backend enforces.

    SQLite compares a UUID column against a text stand-in without complaint and
    PostgreSQL refuses it, so a grouping written without a cast passes the cheap
    suite and returns a 500 to every caller in production. That is exactly how
    this shipped, so the proof belongs on the backend that has an opinion.
    """

    database_url = migrated_database_url.render_as_string(hide_password=False)
    database = DatabaseClient.from_settings(
        DatabaseSettings(url=database_url, application_name=DatabaseApplicationName.Test)
    )
    try:
        with database.session() as session:
            workspace = WorkspaceRepository(session).create(name="aggregates")
            repository = TaskRepository(session)
            for status in (TaskStatus.Complete, TaskStatus.Complete, TaskStatus.Failed):
                repository.upsert(
                    Task(
                        id=str(uuid4()),
                        name="run",
                        status=status,
                        workspace_id=workspace.id,
                    ),
                    workspace_id=workspace.id,
                )

        with database.session() as session:
            repository = TaskRepository(session)
            by_deployment = repository.status_tallies_by_deployment(workspace_id=workspace.id)
            tallies = repository.status_tallies(workspace_id=workspace.id)
            samples = repository.creation_samples(workspace_id=workspace.id)

        # Every task here is deployment-less, so the null group is the whole
        # set, and it is reported as stored rather than dropped or renamed.
        assert {entry.deployment_id for entry in by_deployment} == {None}
        assert sum(entry.count for entry in by_deployment) == 3
        assert {entry.status: entry.count for entry in tallies} == {
            TaskStatus.Complete: 2,
            TaskStatus.Failed: 1,
        }
        assert len(samples) == 3
    finally:
        database.engine.dispose()
