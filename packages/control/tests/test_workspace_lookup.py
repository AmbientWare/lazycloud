from __future__ import annotations

from pathlib import Path

import pytest
from control.service import ControlPlaneService
from database.context import ServiceContext
from database.repositories.identity import WorkspaceRepository
from shared.errors import NotFoundError
from tests.workspaces import owned_workspace

from database import DatabaseClient


def test_control_workspace_reads_are_empty_or_not_found_without_creating_rows(
    database: DatabaseClient,
    tmp_path: Path,
) -> None:
    context = ServiceContext.create(database, root=tmp_path, create_schema=False)
    service = ControlPlaneService(context)
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
        assert [workspace.id for workspace in WorkspaceRepository(session).list()] == [created.id]
