from __future__ import annotations

from uuid import uuid4

import pytest
from alembic import command
from database.migrations import alembic_config
from database.tables.apps import StubTable
from database.tables.identity import WorkspaceTable
from database.tables.orchestration import ContainerTable
from database.tables.previews import PreviewSessionTable
from shared.timestamps import utc_now
from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError


def test_preview_migration_preserves_sources_and_recoverable_container_ownership(
    postgres_database_url: URL,
) -> None:
    url = postgres_database_url.render_as_string(hide_password=False)
    config = alembic_config(url)
    command.upgrade(config, "0046_function_deadlines")
    engine = create_engine(url)
    workspace, source, execution, container, preview = (str(uuid4()) for _ in range(5))
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(WorkspaceTable).values(id=workspace, name="preview-migration", payload={})
            )
            connection.execute(
                insert(StubTable),
                [
                    {
                        "id": source,
                        "workspace_id": workspace,
                        "name": "source",
                        "type": "endpoint",
                        "payload": {"marker": "preserved"},
                    },
                    {
                        "id": execution,
                        "workspace_id": workspace,
                        "name": "execution",
                        "type": "endpoint",
                        "payload": {},
                    },
                ],
            )
            connection.execute(
                insert(ContainerTable).values(
                    id=container,
                    workspace_id=workspace,
                    stub_id=execution,
                    name="preview",
                    image="image-web",
                    status="pending",
                    payload={},
                )
            )
        command.upgrade(config, "0047_preview_sessions")
        with engine.begin() as connection:
            connection.execute(
                insert(PreviewSessionTable).values(
                    id=preview,
                    workspace_id=workspace,
                    source_stub_id=source,
                    execution_stub_id=execution,
                    container_id=container,
                    status="active",
                    public=False,
                )
            )
        with pytest.raises(DBAPIError, match="stop previews"):
            command.downgrade(config, "0046_function_deadlines")
        with engine.begin() as connection:
            connection.execute(delete(StubTable).where(StubTable.id == execution))
            retained = connection.execute(
                select(
                    PreviewSessionTable.container_id, PreviewSessionTable.execution_stub_id
                ).where(PreviewSessionTable.id == preview)
            ).one()
            assert retained == (container, None)
            connection.execute(
                update(PreviewSessionTable)
                .where(PreviewSessionTable.id == preview)
                .values(status="stopped", ended_at=utc_now())
            )
        with pytest.raises(DBAPIError, match="stop previews"):
            command.downgrade(config, "0046_function_deadlines")
        with engine.begin() as connection:
            connection.execute(
                update(ContainerTable)
                .where(ContainerTable.id == container)
                .values(status="stopped")
            )
            connection.execute(
                insert(StubTable).values(
                    id=execution,
                    workspace_id=workspace,
                    name="execution",
                    type="endpoint",
                    payload={},
                )
            )
            connection.execute(
                update(PreviewSessionTable)
                .where(PreviewSessionTable.id == preview)
                .values(execution_stub_id=execution)
            )
        command.downgrade(config, "0046_function_deadlines")
        with engine.connect() as connection:
            assert connection.scalar(select(StubTable.payload).where(StubTable.id == source)) == {
                "marker": "preserved"
            }
            assert connection.scalar(select(StubTable.id).where(StubTable.id == execution)) is None
            assert (
                connection.scalar(select(ContainerTable.id).where(ContainerTable.id == container))
                == container
            )
        command.upgrade(config, "0047_preview_sessions")
    finally:
        engine.dispose()
