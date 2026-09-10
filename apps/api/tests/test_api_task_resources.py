from __future__ import annotations

from uuid import uuid4

from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.orchestration import ContainerRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import DeploymentSpec
from shared.deployments import StubKind
from shared.function_payloads import FunctionCloudpickleResult
from shared.http.errors import ErrorResponse
from shared.http.tasks import TaskDetailResponse, TaskPageResponse
from shared.identity import TokenKind, WorkspaceRecord
from shared.tasks import Task, TaskStatus


def _headers(
    services: ApiServices, workspace_id: str, name: str, *, scopes: list[str]
) -> dict[str, str]:
    token, _ = AuthService(services.context).create_token(
        name,
        scopes=scopes,
        kind=TokenKind.Workspace,
        workspace_id=workspace_id,
    )
    return {"Authorization": f"Bearer {token}"}


def test_raw_task_creation_is_unsupported_and_historic_commands_cannot_rerun(
    api_runtime: tuple[ApiServices, TestClient],
    api_workspace: WorkspaceRecord,
) -> None:
    services, client = api_runtime
    workspace_id = api_workspace.id
    historic = services.tasks.create(
        "historic-command",
        workspace_id=workspace_id,
        command=["python", "-c", "print('unsafe')"],
    )
    historic = services.tasks.transition(historic, TaskStatus.Complete, exit_code=0)
    headers = _headers(
        services, api_workspace.id, "historic-command-write", scopes=["read", "write"]
    )

    unsupported = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"command": ["python", "-c", "print('unsafe')"]},
    )
    assert unsupported.status_code == 405

    rerun = client.post(f"/api/v1/tasks/{historic.id}/rerun", headers=headers)
    assert rerun.status_code == 400
    assert ErrorResponse.model_validate_json(rerun.content).detail == (
        "historic command tasks cannot be re-run"
    )
    assert [
        task.id
        for task in services.tasks.list(workspace_id=api_workspace.id)
        if task.name == historic.name
    ] == [historic.id]


def test_task_detail_projects_durable_function_result_to_public_result(
    api_runtime: tuple[ApiServices, TestClient],
    api_workspace: WorkspaceRecord,
) -> None:
    services, client = api_runtime
    task = services.tasks.create("function-result", workspace_id=api_workspace.id)
    function_result = FunctionCloudpickleResult.from_bytes(b"opaque-python-result")
    services.tasks.transition(
        task,
        TaskStatus.Complete,
        function_result=function_result,
        exit_code=0,
    )
    headers = _headers(services, api_workspace.id, "function-result-reader", scopes=["read"])

    response = client.get(f"/api/v1/tasks/{task.id}", headers=headers)

    assert response.status_code == 200
    payload = TaskDetailResponse.model_validate_json(response.content)
    assert payload.result == function_result.model_dump(mode="json")


def _deployed_task(services: ApiServices, workspace_id: str) -> Task:
    """A task bound to a real app, workload, deployment, and container."""

    deployment = services.deployments.deploy(
        DeploymentSpec(name="reindex", handler="jobs.search:reindex"), workspace=workspace_id
    )
    stub = next(
        item
        for item in ControlPlaneService(services.context).list_stubs(workspace=workspace_id)
        if item.deployment_id == deployment.id
    )
    container = ContainerRecord(
        id=str(uuid4()),
        name="reindex-container",
        image="img-reindex",
        command=["python3.12", "-m", "runner.function"],
        workspace_id=stub.workspace_id,
        app_id=deployment.app_id,
        stub_id=stub.id,
        status=ContainerStatus.Running,
    )
    with services.context.database.session() as session:
        ContainerRepository(session).upsert(container)
    return services.tasks.create(
        "reindex-run",
        workspace_id=stub.workspace_id,
        app_id=deployment.app_id,
        stub_id=stub.id,
        deployment_id=deployment.id,
        container_id=container.id,
    )


def test_task_rows_name_their_resources_and_only_the_detail_read_carries_the_container(
    api_runtime: tuple[ApiServices, TestClient],
    api_workspace: WorkspaceRecord,
) -> None:
    services, client = api_runtime
    task = _deployed_task(services, api_workspace.id)
    headers = _headers(services, api_workspace.id, "task-row-reader", scopes=["read"])

    page = client.get("/api/v1/tasks", headers=headers, params={"limit": 100})
    detail = client.get(f"/api/v1/tasks/{task.id}", headers=headers)

    assert page.status_code == 200
    assert detail.status_code == 200
    row = next(item for item in TaskPageResponse.model_validate_json(page.content).data)
    assert row.id == task.id
    assert row.app is not None and row.app.name == "reindex"
    assert row.workload is not None and row.workload.kind is StubKind.Function
    assert row.deployment is not None and row.deployment.version == 1
    assert "container" not in page.json()["data"][0]

    detail_payload = TaskDetailResponse.model_validate_json(detail.content)
    assert detail_payload.container is not None
    assert detail_payload.container.id == task.container_id
    assert detail_payload.workload == row.workload
