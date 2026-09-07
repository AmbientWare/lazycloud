from datetime import UTC, datetime
from uuid import uuid4

from database.repositories.apps import DeploymentResourceRepository
from database.tables.apps import AppTable, DeploymentTable, StubTable
from database.tables.identity import WorkspaceTable
from database.tables.orchestration import ContainerTable
from shared.deployments import DeploymentKind
from sqlalchemy import insert
from sqlalchemy.engine import URL

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings, bootstrap_database


def test_workload_current_version_counts_and_filters_are_independent_of_history(
    postgres_database_url: URL,
) -> None:
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
    workspace_id, app_id, old_stub_id, current_stub_id = (str(uuid4()) for _ in range(4))
    try:
        with database.session() as session:
            session.execute(
                insert(WorkspaceTable).values(
                    id=workspace_id, name="workloads", status="active", payload={}
                )
            )
            session.execute(
                insert(AppTable).values(
                    id=app_id,
                    workspace_id=workspace_id,
                    name="app",
                    lifecycle_state="active",
                    payload={},
                )
            )
            for stub_id in (old_stub_id, current_stub_id):
                session.execute(
                    insert(StubTable).values(
                        id=stub_id,
                        workspace_id=workspace_id,
                        app_id=app_id,
                        name="alpha",
                        type="function",
                        payload={},
                    )
                )
            session.execute(
                insert(DeploymentTable).values(
                    [
                        dict(
                            id=str(uuid4()),
                            workspace_id=workspace_id,
                            app_id=app_id,
                            stub_id=current_stub_id if version == 302 else old_stub_id,
                            name="alpha",
                            kind="function",
                            version=version,
                            active=version != 302,
                            subdomain="alpha",
                            payload={"version": version},
                            deleted_at=datetime.now(UTC) if version == 303 else None,
                        )
                        for version in range(1, 304)
                    ]
                )
            )
            session.execute(
                insert(DeploymentTable).values(
                    id=str(uuid4()),
                    workspace_id=workspace_id,
                    app_id=app_id,
                    stub_id=current_stub_id,
                    name="beta",
                    kind="pod",
                    version=1,
                    subdomain="beta",
                    payload={"name": "beta"},
                )
            )
            for status in ("running", "pending", "stopped"):
                session.execute(
                    insert(ContainerTable).values(
                        id=str(uuid4()),
                        workspace_id=workspace_id,
                        app_id=app_id,
                        stub_id=old_stub_id,
                        name=status,
                        image="test",
                        status=status,
                        payload={},
                    )
                )
            session.commit()
        with database.session() as session:
            repository = DeploymentResourceRepository(session)
            first = repository.workloads(workspace_id=workspace_id, app_id=app_id, limit=1)
            assert first[0].resource.deployment_payload["version"] == 302
            assert first[0].version_count == 302
            assert first[0].running_containers == 1
            assert first[0].active_containers == 2
            assert (
                repository.workloads(workspace_id=workspace_id, app_id=app_id, after="alpha")[
                    0
                ].resource.deployment_payload["name"]
                == "beta"
            )
            assert (
                repository.workloads(
                    workspace_id=workspace_id, app_id=app_id, kind=DeploymentKind.Pod
                )[0].resource.deployment_payload["name"]
                == "beta"
            )
            assert (
                repository.workloads(workspace_id=workspace_id, app_id=app_id, name="alpha")[
                    0
                ].version_count
                == 302
            )
            assert repository.workloads(workspace_id=str(uuid4()), app_id=app_id) == []
    finally:
        database.dispose()
