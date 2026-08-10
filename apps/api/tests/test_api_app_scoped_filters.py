from __future__ import annotations

from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from fastapi.testclient import TestClient
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.deployments import DeploymentListResponse
from shared.http.stubs import StubListResponse
from shared.http.tasks import TaskTimeWindowBucketListResponse
from shared.tasks import TaskStatus
from tests.service_fixtures import administrator_credential


def _client(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> tuple[TestClient, dict[str, str]]:
    raw_token, _ = administrator_credential(isolated_services, "filters-admin")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    return client, {"Authorization": f"Bearer {raw_token}"}


@pytest.mark.parametrize("resource", ["stubs", "deployments"])
def test_app_scoped_resource_lists_exclude_peer_apps(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    resource: str,
) -> None:
    if resource == "stubs":
        control = ControlPlaneService(isolated_services.context)
        app = isolated_services.apps.create("scoped_list_app")
        expected = control.create_stub("scoped-stub", app_id=app.id)
        control.create_stub("peer-stub")
        response_type = StubListResponse
        items_field = "stubs"
    else:
        expected = isolated_services.deployments.deploy(
            DeploymentSpec(
                name="scoped-deployment",
                kind=DeploymentKind.Endpoint,
                handler="scoped:handler",
                metadata={"app": "scoped_list_app"},
            )
        )
        isolated_services.deployments.deploy(
            DeploymentSpec(
                name="peer-deployment",
                kind=DeploymentKind.Endpoint,
                handler="peer:handler",
                metadata={"app": "peer_list_app"},
            )
        )
        assert expected.app_id is not None
        app = isolated_services.apps.get(expected.app_id)
        response_type = DeploymentListResponse
        items_field = "data"

    client, headers = _client(isolated_services, client_stack)
    response = client.get(
        f"/api/v1/{resource}",
        headers=headers,
        params={"app_id": app.id},
    )

    assert response.status_code == 200, response.text
    payload = response_type.model_validate_json(response.content)
    items = getattr(payload, items_field)
    assert [item.id for item in items] == [expected.id]
    assert items[0].app_id == app.id


def test_deployment_pages_are_app_and_workload_scoped_with_opaque_cursors(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    versions = [
        isolated_services.deployments.deploy(
            DeploymentSpec(
                name="paged-worker",
                kind=DeploymentKind.Function,
                handler="workers:run",
                metadata={"app": "paged_app"},
            )
        )
        for _ in range(5)
    ]
    unrelated = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="paged-worker",
            kind=DeploymentKind.Function,
            handler="workers:run",
            metadata={"app": "other_paged_app"},
        )
    )
    app_id = versions[0].app_id
    assert app_id is not None
    assert unrelated.app_id != app_id

    client, headers = _client(isolated_services, client_stack)
    cursor = ""
    received_ids: list[str] = []
    page_sizes: list[int] = []
    while True:
        response = client.get(
            "/api/v1/deployments",
            headers=headers,
            params={
                "app_id": app_id,
                "name": "paged-worker",
                "limit": 2,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        assert response.status_code == 200, response.text
        payload = DeploymentListResponse.model_validate_json(response.content)
        page_sizes.append(len(payload.data))
        received_ids.extend(item.id for item in payload.data)
        cursor = payload.next
        if not cursor:
            break
        assert not cursor.isdigit()

    assert page_sizes == [2, 2, 1]
    assert received_ids == [deployment.id for deployment in reversed(versions)]
    assert unrelated.id not in received_ids
    assert len(received_ids) == len(set(received_ids)) == 5

    invalid = client.get(
        "/api/v1/deployments",
        headers=headers,
        params={"app_id": app_id, "name": "paged-worker", "cursor": "not-a-cursor"},
    )
    assert invalid.status_code == 400


def test_aggregate_tasks_by_time_window_filters_by_stub_id(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    app = isolated_services.apps.create("aggregate_app")
    first_stub = control.create_stub("aggregate-first", app_id=app.id)
    second_stub = control.create_stub("aggregate-second", app_id=app.id)

    def seed(name: str, *, app_id: str | None, stub_id: str | None) -> None:
        task = isolated_services.tasks.create(
            name,
            workspace_id=None,
            app_id=app_id,
            stub_id=stub_id,
            command=[],
        )
        task.status = TaskStatus.Complete
        isolated_services.tasks.save(task)

    seed("first-run", app_id=app.id, stub_id=first_stub.id)
    seed("first-run-again", app_id=app.id, stub_id=first_stub.id)
    seed("second-run", app_id=app.id, stub_id=second_stub.id)
    seed("unscoped-run", app_id=None, stub_id=None)

    client, headers = _client(isolated_services, client_stack)

    def bucket_total(params: dict[str, str | int]) -> int:
        response = client.get(
            "/api/v1/tasks/aggregate-by-time-window",
            headers=headers,
            params={"window_seconds": 3600, **params},
        )
        assert response.status_code == 200
        payload = TaskTimeWindowBucketListResponse.model_validate_json(response.content)
        return sum(item.count for item in payload.items)

    assert bucket_total({}) == 4
    assert bucket_total({"stub_id": first_stub.id}) == 2
    assert bucket_total({"stub_id": second_stub.id}) == 1
    assert bucket_total({"app_id": app.id}) == 3
    assert bucket_total({"app_id": app.id, "stub_id": first_stub.id}) == 2
