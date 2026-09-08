from __future__ import annotations

import json
from uuid import uuid4

from alembic import command
from database.migrations import alembic_config, bootstrap_database
from database.repositories.apps import DeploymentRepository, StubRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.apps import DeploymentTable, StubTable
from database.tables.identity import WorkspaceTable
from database.tables.orchestration import ContainerTable
from pydantic import JsonValue
from shared.lifecycle import LifecycleHooks
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session


def test_hook_migration_cleans_durable_configs_and_preserves_custom_data(
    postgres_database_url: URL,
) -> None:
    database_url = postgres_database_url.render_as_string(hide_password=False)
    command.upgrade(alembic_config(database_url), "0014_stub_lifecycle_fields")
    engine = create_engine(database_url)
    try:
        (
            workspace_id,
            other_workspace_id,
            stub_id,
            pod_id,
            deployment_id,
            container_id,
            pod_container_id,
        ) = (str(uuid4()) for _ in range(7))
        retained: dict[str, JsonValue] = {
            "on_start": ["handlers:start"],
            "on_finish": ["handlers:finish"],
        }
        legacy: dict[str, JsonValue] = {
            **retained,
            "on_cancelled": ["old:cancel"],
            "on_timeout": ["old:timeout"],
        }
        metadata: dict[str, JsonValue] = {"on_cancelled": "custom", "lifecycle_hooks": legacy}
        stub: dict[str, JsonValue] = {
            "id": stub_id,
            "workspace_id": workspace_id,
            "name": "function",
            "config": {"lifecycle_hooks": legacy, "metadata": metadata, "pool": "customer-aws"},
        }
        deployment: dict[str, JsonValue] = {
            "id": deployment_id,
            "name": "function",
            "kind": "function",
            "subdomain": "function-test",
            "spec": {
                "name": "function",
                "kind": "function",
                "lifecycle_hooks": legacy,
                "metadata": metadata,
            },
        }
        container: dict[str, JsonValue] = {
            "id": container_id,
            "workspace_id": workspace_id,
            "stub_id": stub_id,
            "name": "function",
            "image": "test-image",
            "command": [],
            "env": {"LIFECYCLE_HOOKS": json.dumps(legacy), "USER_VALUE": "on_timeout"},
        }
        pod: dict[str, JsonValue] = {
            "id": pod_id,
            "workspace_id": other_workspace_id,
            "name": "pod",
            "kind": "pod",
            "config": {"metadata": metadata},
        }
        pod_container: dict[str, JsonValue] = {
            **container,
            "id": pod_container_id,
            "workspace_id": other_workspace_id,
            "stub_id": pod_id,
        }
        with engine.begin() as connection:
            connection.execute(
                insert(WorkspaceTable),
                [
                    {"id": workspace_id, "name": "hooks", "payload": {}},
                    {"id": other_workspace_id, "name": "other", "payload": {}},
                ],
            )
            connection.execute(
                insert(StubTable),
                [
                    {
                        "id": stub_id,
                        "workspace_id": workspace_id,
                        "name": "function",
                        "type": "function",
                        "payload": stub,
                    },
                    {
                        "id": pod_id,
                        "workspace_id": other_workspace_id,
                        "name": "pod",
                        "type": "pod",
                        "payload": pod,
                    },
                ],
            )
            connection.execute(
                insert(DeploymentTable).values(
                    id=deployment_id,
                    workspace_id=workspace_id,
                    name="function",
                    kind="function",
                    subdomain="function-test",
                    payload=deployment,
                )
            )
            connection.execute(
                insert(ContainerTable),
                [
                    {
                        "id": container_id,
                        "workspace_id": workspace_id,
                        "stub_id": stub_id,
                        "name": "function",
                        "image": "test-image",
                        "status": "pending",
                        "payload": container,
                    },
                    {
                        "id": pod_container_id,
                        "workspace_id": other_workspace_id,
                        "stub_id": pod_id,
                        "name": "pod",
                        "image": "test-image",
                        "status": "pending",
                        "payload": pod_container,
                    },
                ],
            )

        bootstrap_database(database_url)
        expected_hooks = LifecycleHooks.model_validate(retained)
        with Session(engine) as session:
            assert session.scalar(select(StubTable.payload).where(StubTable.id == stub_id)) == {
                **stub,
                "config": {
                    "lifecycle_hooks": retained,
                    "metadata": metadata,
                    "pool": "customer-aws",
                },
            }
            assert session.scalar(
                select(DeploymentTable.payload).where(DeploymentTable.id == deployment_id)
            ) == {
                **deployment,
                "spec": {
                    "name": "function",
                    "kind": "function",
                    "lifecycle_hooks": retained,
                    "metadata": metadata,
                },
            }
            assert session.scalar(
                select(ContainerTable.payload).where(ContainerTable.id == container_id)
            ) == {
                **container,
                "env": {
                    "LIFECYCLE_HOOKS": json.dumps(retained, separators=(",", ":")),
                    "USER_VALUE": "on_timeout",
                },
            }
            assert session.scalar(select(StubTable.payload).where(StubTable.id == pod_id)) == pod
            assert (
                session.scalar(
                    select(ContainerTable.payload).where(ContainerTable.id == pod_container_id)
                )
                == pod_container
            )
            loaded_stub = StubRepository(session).get(stub_id, workspace_id=workspace_id)
            assert loaded_stub is not None
            assert loaded_stub.config.lifecycle_hooks == expected_hooks
            loaded_deployment = DeploymentRepository(session).records.get(
                deployment_id, workspace_id=workspace_id
            )
            assert loaded_deployment is not None
            assert loaded_deployment.spec.lifecycle_hooks == expected_hooks
            loaded_container = ContainerRepository(session).get(
                container_id, workspace_id=workspace_id
            )
            assert loaded_container is not None
            assert (
                LifecycleHooks.model_validate_json(loaded_container.env["LIFECYCLE_HOOKS"])
                == expected_hooks
            )
    finally:
        engine.dispose()
