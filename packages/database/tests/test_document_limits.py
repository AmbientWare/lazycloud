import pytest
from database.context import ServiceContext
from database.repositories.identity import WorkspaceRepository
from shared.errors import InvalidInputError


def test_oversized_metadata_is_rejected_without_partial_persistence(
    committed_service_context: ServiceContext,
) -> None:
    database = committed_service_context.database
    with database.session() as session:
        workspace = WorkspaceRepository(session).create(name="document-limit")
    with (
        pytest.raises(InvalidInputError, match="document exceeds"),
        database.session() as session,
    ):
        workspace.metadata = {"value": "x" * (1024 * 1024)}
        WorkspaceRepository(session).upsert(workspace)
    with database.session() as session:
        stored = WorkspaceRepository(session).get(workspace.id)
        assert stored is not None
        assert stored.metadata == {}
