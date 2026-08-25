from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from database.repositories.apps import AppSummaryRepository
from database.tables.apps import AppTable
from database.tables.execution import TaskTable
from database.tables.identity import WorkspaceTable
from shared.tasks import TaskStatus
from sqlalchemy import insert
from sqlalchemy.engine import URL

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings, bootstrap_database


def test_the_hourly_summary_counts_success_rather_than_inferring_it(
    postgres_database_url: URL,
) -> None:
    """What the app card paints each hour with.

    Every status in one hour, because the failure this guards against is not an
    empty chart. It is a chart that looks right: succeeded is the band a reader
    trusts, and a cancelled or unrecognised task counted into it reports work
    that nobody finished as work that came out fine.
    """
    dsn = postgres_database_url.render_as_string(hide_password=False)
    bootstrap_database(dsn)
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=dsn,
            pool_size=4,
            max_overflow=0,
            statement_timeout_ms=0,
            application_name=DatabaseApplicationName.Test,
        )
    )
    try:
        hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        workspace_id, app_id = _seed(database, hour)

        with database.session() as session:
            summaries = AppSummaryRepository(session).execution_summaries(
                workspace_id=workspace_id,
                start=hour - timedelta(hours=23),
            )

        summary = summaries[app_id]
        assert summary.runs_24h == 7
        assert summary.succeeded_runs_24h == 2
        assert summary.failed_runs_24h == 2
        assert summary.pending_runs_24h == 2
        # The seventh is cancelled, and belongs to none of the three.
        assert sum(summary.succeeded_24h) == 2
        assert summary.succeeded_24h[-1] == 2
    finally:
        database.dispose()


def _seed(database: DatabaseClient, hour: datetime) -> tuple[str, str]:
    workspace_id = str(uuid4())
    app_id = str(uuid4())
    statuses = [
        TaskStatus.Complete,
        TaskStatus.Complete,
        TaskStatus.Failed,
        TaskStatus.Timeout,
        TaskStatus.Pending,
        TaskStatus.Running,
        TaskStatus.Cancelled,
    ]
    with database.session() as session:
        session.execute(
            insert(WorkspaceTable).values(
                id=workspace_id,
                name="summaries",
                status="active",
                payload={},
            )
        )
        session.execute(
            insert(AppTable).values(
                id=app_id,
                workspace_id=workspace_id,
                name="activity",
                lifecycle_state="active",
                payload={},
            )
        )
        session.execute(
            insert(TaskTable).values(
                [
                    {
                        "id": str(uuid4()),
                        "workspace_id": workspace_id,
                        "app_id": app_id,
                        "name": f"task-{index}",
                        "status": status.value,
                        "created_at": hour + timedelta(minutes=index),
                        "payload": {},
                    }
                    for index, status in enumerate(statuses)
                ]
            )
        )
        session.commit()
    return workspace_id, app_id
