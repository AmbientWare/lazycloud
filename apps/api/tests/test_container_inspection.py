from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.orchestration import ContainerRepository
from database.tables.orchestration import ContainerTable
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.containers import ContainerRecord, ContainerStatus
from shared.http.compute import ContainerWithAppPageResponse
from shared.identity import TokenKind, WorkspaceRecord
from sqlalchemy import update
from tests.workspaces import owned_workspace


def test_canonical_container_pages_are_bounded_stable_and_secret_free(
    api_runtime: tuple[ApiServices, TestClient],
    api_workspace: WorkspaceRecord,
) -> None:
    services, client = api_runtime
    workspace_id = api_workspace.id
    marker = "container-inspection-secret-marker"
    created_at = datetime(2026, 7, 12, 12, tzinfo=UTC)
    expected_ids = [
        str(uuid5(NAMESPACE_URL, f"lazycloud:gateway-container:{index}")) for index in range(5)
    ]
    foreign_workspace = owned_workspace(
        ControlPlaneService(services.context), "container-inspection-foreign"
    )
    with services.context.database.session() as session:
        repository = ContainerRepository(session)
        for index, container_id in enumerate(expected_ids):
            repository.upsert(
                ContainerRecord(
                    id=container_id,
                    name=f"gateway-container-{index:03d}",
                    image="python:3.12",
                    command=["python", "worker.py"],
                    workspace_id=workspace_id,
                    status=ContainerStatus.Running,
                    env={"GATEWAY_TOKEN": marker, "ORDINARY_VALUE": "not-public"},
                    created_at=created_at,
                )
            )
        repository.upsert(
            ContainerRecord(
                id=str(uuid5(NAMESPACE_URL, "lazycloud:gateway-container:foreign")),
                name="foreign-container",
                image="python:3.12",
                command=[],
                workspace_id=foreign_workspace.id,
                status=ContainerStatus.Running,
                env={"GATEWAY_TOKEN": "foreign-secret-marker"},
                created_at=created_at,
            )
        )
        session.execute(
            update(ContainerTable)
            .where(ContainerTable.id.in_(expected_ids))
            .values(created_at=created_at)
        )

    token, _ = AuthService(services.context).create_token(
        "container-inspection",
        kind=TokenKind.Workspace,
        workspace_id=workspace_id,
    )
    headers = {"Authorization": f"Bearer {token}"}

    cursor = ""
    received_ids: list[str] = []
    page_sizes: list[int] = []
    seen_cursors: set[str] = set()
    for _ in range(len(expected_ids) + 1):
        response = client.get(
            "/api/v1/containers",
            headers=headers,
            params={"limit": 2, "cursor": cursor, "status": "running"},
        )
        assert response.status_code == 200, response.text
        assert marker not in response.text
        assert "GATEWAY_TOKEN" not in response.text
        payload = ContainerWithAppPageResponse.model_validate_json(response.content)
        page_ids = [item.container.id for item in payload.data]
        page_sizes.append(len(page_ids))
        received_ids.extend(page_ids)
        cursor = payload.next
        if not cursor:
            break
        assert cursor not in seen_cursors
        seen_cursors.add(cursor)

    assert cursor == ""
    assert page_sizes == [2, 2, 1]
    assert received_ids == sorted(expected_ids, reverse=True)
    assert len(received_ids) == len(set(received_ids)) == 5

    oversized = client.get(
        "/api/v1/containers",
        headers=headers,
        params={"limit": 101},
    )
    invalid_cursor = client.get(
        "/api/v1/containers",
        headers=headers,
        params={"cursor": "not-a-cursor"},
    )
    forged_workspace = client.get(
        "/api/v1/containers",
        headers=headers,
        params={"workspace": foreign_workspace.id},
    )
    assert oversized.status_code == 422
    assert invalid_cursor.status_code == 400
    assert forged_workspace.status_code == 403
