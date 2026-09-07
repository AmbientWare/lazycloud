from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.apps import WorkloadPageResponse
from shared.http.observability import TaskLatencyTimeseriesResponse
from shared.http.tasks import TaskPageResponse
from shared.tasks import TaskStatus
from tests.service_fixtures import administrator_credential


def test_workload_deletion_removes_every_version_and_preserves_siblings(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    versions = [
        isolated_services.deployments.deploy(
            DeploymentSpec(name="predict", handler=f"pkg:v{version}", metadata={"app": "demo"})
        )
        for version in range(1, 3)
    ]
    sibling = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="predict",
            kind=DeploymentKind.Endpoint,
            handler="pkg:sibling",
            metadata={"app": "demo"},
        )
    )
    raw_token, _ = administrator_credential(isolated_services, "workload-admin")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {"Authorization": f"Bearer {raw_token}"}
    path = f"/api/v1/apps/{versions[-1].app_id}/workloads"
    assert versions[-1].app_id is not None
    workspace_id = isolated_services.apps.get(versions[-1].app_id).workspace_id
    function_tasks: set[str] = set()
    for deployment in [*versions, sibling]:
        task = isolated_services.tasks.create(
            "predict",
            workspace_id=workspace_id,
            app_id=deployment.app_id,
            stub_id=deployment.stub_id,
            deployment_id=deployment.id,
        )
        task = isolated_services.tasks.transition(task, TaskStatus.Running)
        isolated_services.tasks.transition(task, TaskStatus.Complete)
        if deployment.kind is DeploymentKind.Function:
            function_tasks.add(task.id)
    task_page = client.get(
        "/api/v1/tasks",
        params={"app_id": versions[-1].app_id, "workload_name": "predict", "kind": "function"},
        headers=headers,
    )
    assert task_page.status_code == 200
    assert {
        task.id for task in TaskPageResponse.model_validate_json(task_page.content).data
    } == function_tasks
    latency = client.get(
        "/api/v1/metrics/task-latency",
        params={
            "app_id": versions[-1].app_id,
            "workload_name": "predict",
            "workload_kind": "function",
        },
        headers=headers,
    )
    assert latency.status_code == 200
    assert (
        sum(
            bucket.count
            for bucket in TaskLatencyTimeseriesResponse.model_validate_json(latency.content).buckets
        )
        == 2
    )
    response = client.get(
        path, params={"name": "predict", "kind": DeploymentKind.Function.value}, headers=headers
    )
    assert response.status_code == 200
    page = WorkloadPageResponse.model_validate_json(response.content)
    assert page.data[0].deployment.id == versions[-1].id
    assert page.data[0].version_count == 2
    assert client.delete(f"{path}/predict", headers=headers).status_code == 422
    first = client.get(path, params={"limit": 1}, headers=headers)
    assert first.status_code == 200
    first_page = WorkloadPageResponse.model_validate_json(first.content)
    assert first_page.data[0].deployment.id == sibling.id
    assert first_page.data[0].version_count == 1
    second = client.get(path, params={"limit": 1, "cursor": first_page.next}, headers=headers)
    assert second.status_code == 200
    second_page = WorkloadPageResponse.model_validate_json(second.content)
    assert second_page.data[0].deployment.id == versions[-1].id
    assert not second_page.next
    history = client.get(
        "/api/v1/deployments",
        params={"app_id": versions[-1].app_id, "name": "predict", "kind": "function"},
        headers=headers,
    )
    assert history.status_code == 200
    assert {item["id"] for item in history.json()["data"]} == {version.id for version in versions}
    deleted = client.delete(f"{path}/predict", params={"kind": "function"}, headers=headers)
    assert deleted.status_code == 204
    remaining = client.get(path, headers=headers)
    assert remaining.status_code == 200
    page = WorkloadPageResponse.model_validate_json(remaining.content)
    assert [item.deployment.id for item in page.data] == [sibling.id]
    assert (
        client.delete(f"{path}/predict", params={"kind": "function"}, headers=headers).status_code
        == 204
    )
