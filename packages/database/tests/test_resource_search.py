from __future__ import annotations

from uuid import uuid4

from database.repositories.search import ResourceSearchRepository
from database.tables.apps import AppTable, DeploymentTable, StubTable
from database.tables.execution import TaskTable
from database.tables.identity import WorkspaceTable
from database.tables.orchestration import ContainerTable
from sqlalchemy import insert
from sqlalchemy.engine import URL

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings, bootstrap_database


def test_resource_search_pages_all_kinds_without_crossing_workspaces(
    postgres_database_url: URL,
) -> None:
    dsn = postgres_database_url.render_as_string(hide_password=False)
    bootstrap_database(dsn)
    database = DatabaseClient.from_settings(
        DatabaseSettings(url=dsn, application_name=DatabaseApplicationName.Test)
    )
    owner, other = str(uuid4()), str(uuid4())
    expected: set[tuple[str, str]] = set()
    try:
        with database.session() as session:
            for workspace in (owner, other):
                app_id, stub_id = str(uuid4()), str(uuid4())
                session.execute(
                    insert(WorkspaceTable).values(
                        id=workspace, name=workspace, status="active", payload={}
                    )
                )
                session.execute(
                    insert(AppTable).values(
                        id=app_id, workspace_id=workspace, name="match-app", payload={}
                    )
                )
                session.execute(
                    insert(StubTable).values(
                        id=stub_id,
                        workspace_id=workspace,
                        app_id=app_id,
                        name="match-sandbox",
                        type="sandbox",
                        payload={},
                    )
                )
                for version, kind in ((1, "pod"), (2, "pod"), (1, "function")):
                    session.execute(
                        insert(DeploymentTable).values(
                            id=str(uuid4()),
                            workspace_id=workspace,
                            app_id=app_id,
                            stub_id=stub_id,
                            name="match-workload",
                            kind=kind,
                            version=version,
                            subdomain=uuid4().hex,
                            payload={},
                        )
                    )
                task_id = str(uuid4())
                session.execute(
                    insert(TaskTable).values(
                        id=task_id,
                        workspace_id=workspace,
                        app_id=app_id,
                        name="match-task",
                        status="complete",
                        payload={},
                    )
                )
                if workspace == owner:
                    expected.update(
                        {
                            ("app", app_id),
                            ("task", task_id),
                            ("workload", f"{app_id}/pod/match-workload"),
                            ("workload", f"{app_id}/function/match-workload"),
                        }
                    )
                for index in range(61):
                    container_id = str(uuid4())
                    session.execute(
                        insert(ContainerTable).values(
                            id=container_id,
                            workspace_id=workspace,
                            stub_id=stub_id,
                            app_id=app_id,
                            name=f"match-sandbox-{index:03}",
                            image="test",
                            status="running",
                            payload={},
                        )
                    )
                    if workspace == owner:
                        expected.add(("sandbox", container_id))
        after: tuple[str, str, str] | None = None
        found: list[tuple[str, str]] = []
        with database.session() as session:
            repository = ResourceSearchRepository(session)
            for _ in range(10):
                page = repository.search(owner, "match", after=after, limit=15)
                found.extend((item.kind.value, item.id) for item in page)
                if len(page) < 15:
                    break
                last = page[-1]
                after = last.kind.value, last.name, last.id
            assert len(found) == len(expected)
            assert set(found) == expected
            assert repository.search(owner, "%", after=None, limit=15) == []
    finally:
        database.dispose()
