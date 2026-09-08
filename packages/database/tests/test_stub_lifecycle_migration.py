from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from alembic import command
from database.migrations import alembic_config, bootstrap_database
from database.repositories.apps import StubRepository
from database.tables.apps import StubTable
from database.tables.identity import WorkspaceTable
from pydantic import JsonValue
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session


def test_stub_lifecycle_migration_preserves_current_hooks_and_other_tenant_data(
    postgres_database_url: URL,
) -> None:
    database_url = postgres_database_url.render_as_string(hide_password=False)
    command.upgrade(alembic_config(database_url), "0013_artifact_storage")
    engine = create_engine(database_url)
    try:
        workspace_id, other_workspace_id, stub_id, other_stub_id = (str(uuid4()) for _ in range(4))
        config: dict[str, JsonValue] = {
            "on_start": "obsolete:start",
            "on_deploy": "obsolete:deploy",
            "on_deploy_stub_id": str(uuid4()),
            "lifecycle_hooks": {"on_start": ["handlers:initialize"]},
            "pool": "customer-aws",
            "metadata": {"on_start": "customer label", "on_deploy": "customer value"},
        }
        legacy: dict[str, JsonValue] = {
            "id": stub_id,
            "workspace_id": workspace_id,
            "name": "legacy",
            "kind": "function",
            "config": config,
            "metadata": {"on_deploy_stub_id": "customer metadata"},
        }
        other: dict[str, JsonValue] = {
            "id": other_stub_id,
            "workspace_id": other_workspace_id,
            "name": "other",
            "kind": "function",
            "config": {"lifecycle_hooks": {"on_start": ["other:initialize"]}},
        }
        with engine.begin() as connection:
            connection.execute(
                insert(WorkspaceTable),
                [
                    {"id": workspace_id, "name": "legacy-owner", "payload": {}},
                    {"id": other_workspace_id, "name": "other-owner", "payload": {}},
                ],
            )
            connection.execute(
                insert(StubTable),
                [
                    {
                        "id": stub_id,
                        "workspace_id": workspace_id,
                        "name": "legacy",
                        "type": "function",
                        "payload": legacy,
                    },
                    {
                        "id": other_stub_id,
                        "workspace_id": other_workspace_id,
                        "name": "other",
                        "type": "function",
                        "payload": other,
                    },
                ],
            )

        bootstrap_database(database_url)
        expected = deepcopy(legacy)
        expected["config"] = {
            key: value
            for key, value in config.items()
            if key not in {"on_start", "on_deploy", "on_deploy_stub_id"}
        }
        with Session(engine) as session:
            assert (
                session.scalar(select(StubTable.payload).where(StubTable.id == stub_id)) == expected
            )
            assert (
                session.scalar(select(StubTable.payload).where(StubTable.id == other_stub_id))
                == other
            )
            loaded = StubRepository(session).get(stub_id, workspace_id=workspace_id)
            assert loaded is not None
            assert loaded.config.lifecycle_hooks.on_start == ("handlers:initialize",)
            assert loaded.config.pool == "customer-aws"
    finally:
        engine.dispose()
