from __future__ import annotations

import json
from uuid import uuid4

import pytest
from database.repositories.identity import WorkspaceRepository
from pydantic import JsonValue
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
    bootstrap_database,
)


@pytest.mark.parametrize(
    ("name", "kind", "payload"),
    [
        ("non-aws", "generic", {"kind": "generic", "config": {}}),
        ("missing-kind", "aws", {"config": {}}),
        ("divergent", "aws", {"kind": "generic", "config": {}}),
        ("wrong-type", "aws", {"kind": 7, "config": {}}),
        ("nested-authority", "aws", {"kind": "aws", "config": {"provider_kind": "aws"}}),
    ],
)
def test_postgresql_provider_constraints_reject_noncanonical_authority(
    postgres_database_url: URL,
    name: str,
    kind: str,
    payload: dict[str, JsonValue],
) -> None:
    database_url = postgres_database_url.render_as_string(hide_password=False)
    bootstrap_database(database_url)
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=database_url,
            application_name=DatabaseApplicationName.Test,
        )
    )
    engine = create_engine(database_url)
    try:
        with database.session() as session:
            workspace_id = WorkspaceRepository(session).create(name=f"provider-{uuid4()}").id
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO providers (
                        id, workspace_id, name, kind, enabled, priority, payload
                    ) VALUES (
                        :id, :workspace_id, :name, :kind, true, 100, CAST(:payload AS JSONB)
                    )
                    """
                ),
                {
                    "id": str(uuid4()),
                    "workspace_id": workspace_id,
                    "name": name,
                    "kind": kind,
                    "payload": json.dumps(payload),
                },
            )
    finally:
        engine.dispose()
        database.dispose()
