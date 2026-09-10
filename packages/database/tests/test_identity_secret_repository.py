from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest
from control.service import ControlPlaneService
from database.context import ServiceContext
from database.repositories.identity import SecretRepository, WorkspaceRepository
from database.tables.identity import WorkspaceTable
from shared.errors import ConflictError, NotFoundError
from sqlalchemy import delete, inspect
from sqlalchemy.engine import URL
from tests.workspaces import owned_workspace

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
)


def test_secret_repository_mutations_have_exact_outcomes_and_stable_identity(
    service_context: ServiceContext,
) -> None:
    workspace = owned_workspace(ControlPlaneService(service_context), "secret-mutation-owner")
    with service_context.database.session() as session:
        repository = SecretRepository(session)
        created = repository.create("API_TOKEN", "ciphertext-1", workspace_id=workspace.id)
        with pytest.raises(ConflictError, match="already exists"):
            repository.create("API_TOKEN", "collision", workspace_id=workspace.id)
        updated = repository.update("API_TOKEN", "ciphertext-2", workspace_id=workspace.id)
        set_record, was_created = repository.set(
            "API_TOKEN",
            "ciphertext-3",
            workspace_id=workspace.id,
        )
        inserted, inserted_created = repository.set(
            "SECOND_TOKEN",
            "ciphertext-4",
            workspace_id=workspace.id,
        )

        assert updated.id == created.id
        assert updated.created_at == created.created_at
        assert set_record.id == created.id
        assert set_record.created_at == created.created_at
        assert was_created is False
        assert inserted.name == "SECOND_TOKEN"
        assert inserted_created is True
        assert [item.name for item in repository.list(workspace_id=workspace.id)] == [
            "API_TOKEN",
            "SECOND_TOKEN",
        ]

        deleted = repository.delete("API_TOKEN", workspace_id=workspace.id)
        assert deleted.id == created.id
        with pytest.raises(NotFoundError, match="secret not found"):
            repository.update("API_TOKEN", "missing", workspace_id=workspace.id)
        with pytest.raises(NotFoundError, match="secret not found"):
            repository.delete("API_TOKEN", workspace_id=workspace.id)


def test_secret_repository_isolates_same_name_and_cascades_workspace_delete(
    service_context: ServiceContext,
) -> None:
    control = ControlPlaneService(service_context)
    first = owned_workspace(control, "secret-isolation-a")
    second = owned_workspace(control, "secret-isolation-b")
    with service_context.database.session() as session:
        repository = SecretRepository(session)
        repository.create("SAME_NAME", "ciphertext-a", workspace_id=first.id)
        repository.create("SAME_NAME", "ciphertext-b", workspace_id=second.id)
        session.execute(delete(WorkspaceTable).where(WorkspaceTable.id == first.id))

    with service_context.database.session() as session:
        repository = SecretRepository(session)
        assert repository.get("SAME_NAME", workspace_id=first.id) is None
        retained = repository.get("SAME_NAME", workspace_id=second.id)
        assert retained is not None
        assert retained.ciphertext == "ciphertext-b"


def test_postgresql_secret_concurrency_and_current_schema(
    migrated_database_url: URL,
) -> None:
    database_url = migrated_database_url.render_as_string(hide_password=False)
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=database_url,
            application_name=DatabaseApplicationName.Test,
        )
    )
    try:
        with database.session() as session:
            workspace = WorkspaceRepository(session).create(name="postgres-secret-owner")
            initial = SecretRepository(session).create(
                "ATOMIC_SET",
                "ciphertext-initial",
                workspace_id=workspace.id,
            )

        barrier = Barrier(8)

        def set_value(index: int) -> tuple[str, datetime, datetime]:
            barrier.wait()
            with database.session() as session:
                record, created = SecretRepository(session).set(
                    "ATOMIC_SET",
                    f"ciphertext-{index}",
                    workspace_id=workspace.id,
                )
                assert created is False
                return record.id, record.created_at, record.updated_at

        with ThreadPoolExecutor(max_workers=8) as executor:
            mutations = list(executor.map(set_value, range(8)))

        create_barrier = Barrier(2)

        def create_once(index: int) -> str:
            create_barrier.wait()
            try:
                with database.session() as session:
                    SecretRepository(session).create(
                        "ATOMIC_CREATE",
                        f"ciphertext-{index}",
                        workspace_id=workspace.id,
                    )
            except ConflictError:
                return "conflict"
            return "created"

        with ThreadPoolExecutor(max_workers=2) as executor:
            create_results = list(executor.map(create_once, range(2)))

        with database.engine.connect() as connection:
            column_names = {
                item["name"] for item in inspect(connection).get_columns("workspace_secrets")
            }
        with database.session() as session:
            final = SecretRepository(session).get("ATOMIC_SET", workspace_id=workspace.id)
    finally:
        database.dispose()

    assert {record_id for record_id, _, _ in mutations} == {initial.id}
    assert {created_at for _, created_at, _ in mutations} == {initial.created_at}
    assert sorted(create_results) == ["conflict", "created"]
    assert final is not None
    assert final.created_at == initial.created_at
    assert final.updated_at == max(updated_at for _, _, updated_at in mutations)
    assert final.ciphertext in {f"ciphertext-{index}" for index in range(8)}
    assert column_names == {
        "id",
        "workspace_id",
        "name",
        "ciphertext",
        "created_at",
        "updated_at",
    }
