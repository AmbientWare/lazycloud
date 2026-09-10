from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from database.context import ServiceContext
from database.repositories.identity import WorkspaceRepository
from shared.errors import NotFoundError

from database import DatabaseClient


def test_service_context_workspace_lookup_never_creates_missing_rows(
    database: DatabaseClient, tmp_path: Path
) -> None:
    context = ServiceContext.create(database, root=tmp_path, create_schema=False)
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
