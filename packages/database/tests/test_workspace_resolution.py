from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from database.context import ServiceContext
from database.repositories.identity import WorkspaceRepository
from shared.errors import NotFoundError

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


def test_service_context_workspace_lookup_never_creates_missing_rows(tmp_path: Path) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            application_name=DatabaseApplicationName.Test,
        )
    )
    context = ServiceContext.create(database, root=tmp_path)
    try:
        for missing in ("default", "missing-workspace", str(uuid4())):
            with database.session() as session:
                assert WorkspaceRepository(session).list() == []
                with pytest.raises(NotFoundError, match=f"workspace not found: {missing}"):
                    context.workspace(session, missing)
                assert WorkspaceRepository(session).list() == []

        with database.session() as session:
            with pytest.raises(NotFoundError, match="workspace not found: default"):
                context.default_workspace_id(session)
            assert WorkspaceRepository(session).list() == []
    finally:
        database.dispose()
