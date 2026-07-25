from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from database.repositories.execution import PodExecutionRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.execution import PodUrlTable
from database.tables.orchestration import ContainerTable
from shared.containers import ContainerRecord, ContainerStatus
from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
    bootstrap_database,
)


def test_pod_url_upsert_preserves_identity_timestamps_port_and_cascade(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'pod-urls.db'}"
    bootstrap_database(database_url)
    database = _client(database_url)
    try:
        container_id = _seed_container(database)
        with database.session() as session:
            first = PodExecutionRepository(session).urls.upsert(
                container_id=container_id,
                port=8080,
                url="https://gateway.example/sandbox/first",
            )
        with database.session() as session:
            same = PodExecutionRepository(session).urls.upsert(
                container_id=container_id,
                port=8080,
                url=first.url,
            )
        with database.session() as session:
            replacement = PodExecutionRepository(session).urls.upsert(
                container_id=container_id,
                port=8080,
                url="https://gateway.example/sandbox/replacement",
            )

        assert same.id == first.id == replacement.id
        assert same.created_at == first.created_at == replacement.created_at
        assert same.updated_at == first.updated_at
        assert replacement.updated_at >= same.updated_at

        with pytest.raises(IntegrityError), database.engine.begin() as connection:
            connection.execute(
                insert(PodUrlTable).values(
                    id=str(uuid4()),
                    container_id=container_id,
                    port=0,
                    url="https://gateway.example/invalid",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        with database.engine.begin() as connection:
            connection.execute(delete(ContainerTable).where(ContainerTable.id == container_id))
            assert connection.scalar(select(func.count()).select_from(PodUrlTable)) == 0
    finally:
        database.dispose()


def test_postgresql_concurrent_pod_url_upsert_preserves_one_identity(
    postgres_database_url: URL,
) -> None:
    database_url = postgres_database_url.render_as_string(hide_password=False)
    bootstrap_database(database_url)
    seed = _client(database_url)
    try:
        container_id = _seed_container(seed)
    finally:
        seed.dispose()

    def replace(index: int) -> str:
        database = _client(database_url)
        try:
            with database.session() as session:
                return (
                    PodExecutionRepository(session)
                    .urls.upsert(
                        container_id=container_id,
                        port=8080,
                        url=f"https://gateway-{index}.example/sandbox/{container_id}/8080",
                    )
                    .id
                )
        finally:
            database.dispose()

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = tuple(executor.map(replace, range(24)))
    verification = _client(database_url)
    try:
        with verification.session() as session:
            rows = PodExecutionRepository(session).urls.list_for_container(container_id)
    finally:
        verification.dispose()

    assert len(set(ids)) == 1
    assert len(rows) == 1
    assert rows[0].id == ids[0]


def _seed_container(database: DatabaseClient) -> str:
    with database.session() as session:
        workspace = WorkspaceRepository(session).create(name=f"pod-url-{uuid4()}")
        container = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="sandbox",
                image="image",
                command=["sleep", "300"],
                workspace_id=workspace.id,
                status=ContainerStatus.Running,
            )
        )
    return container.id


def _client(database_url: str) -> DatabaseClient:
    return DatabaseClient.from_settings(
        DatabaseSettings(
            url=database_url,
            pool_size=8,
            max_overflow=0,
            statement_timeout_ms=0,
            application_name=DatabaseApplicationName.Test,
        )
    )
