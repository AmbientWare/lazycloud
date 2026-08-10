from __future__ import annotations

from pathlib import Path

import pytest
from control.service import ControlPlaneService
from database.context import ServiceContext
from database.repositories.identity import WorkspaceRepository
from shared.errors import NotFoundError
from tests.service_fixtures import owned_workspace

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


def test_control_workspace_reads_are_empty_or_not_found_without_creating_rows(
    tmp_path: Path,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            application_name=DatabaseApplicationName.Test,
        )
    )
    context = ServiceContext.create(database, root=tmp_path)
    service = ControlPlaneService(context)
    try:
        assert service.list_workspaces() == []
        for missing in ("default", "missing-workspace"):
            with pytest.raises(NotFoundError, match=f"workspace not found: {missing}"):
                service.get_workspace(missing)
            with pytest.raises(NotFoundError, match=f"workspace not found: {missing}"):
                service.create_stub("must-not-persist", workspace=missing)

        with database.session() as session:
            assert WorkspaceRepository(session).list() == []

        created = owned_workspace(service, "default")
        assert service.get_workspace("default") == created
        assert service.get_workspace(created.id) == created
        assert service.list_workspaces() == [created]

        with database.session() as session:
            assert [workspace.id for workspace in WorkspaceRepository(session).list()] == [
                created.id
            ]
    finally:
        database.dispose()
