from __future__ import annotations

from contextlib import ExitStack
from uuid import uuid4

from api.fastapi_app import create_app
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
from shared.identity import TokenKind
from shared.tasks import Task, TaskStatus


def _headers(isolated_services: ApiServices, name: str, *, scopes: list[str]) -> dict[str, str]:
    token, _ = AuthService(isolated_services.context).create_token(
        name,
        scopes=scopes,
        kind=TokenKind.Workspace,
    )
    return {"Authorization": f"Bearer {token}"}


def test_raw_task_creation_is_unsupported_and_historic_commands_cannot_rerun(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    historic = isolated_services.tasks.create(
        "historic-command",
        workspace_id=workspace_id,
        command=["python", "-c", "print('unsafe')"],
    )
    historic = isolated_services.tasks.transition(historic, TaskStatus.Complete, exit_code=0)
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = _headers(isolated_services, "historic-command-write", scopes=["read", "write"])

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
    assert [task.id for task in isolated_services.tasks.list() if task.name == historic.name] == [
        historic.id
    ]


def test_task_detail_projects_durable_function_result_to_public_result(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    task = isolated_services.tasks.create("function-result")
    function_result = FunctionCloudpickleResult.from_bytes(b"opaque-python-result")
    isolated_services.tasks.transition(
        task,
        TaskStatus.Complete,
        function_result=function_result,
        exit_code=0,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = _headers(isolated_services, "function-result-reader", scopes=["read"])

    response = client.get(f"/api/v1/tasks/{task.id}", headers=headers)

    assert response.status_code == 200
    payload = TaskDetailResponse.model_validate_json(response.content)
    assert payload.result == function_result.model_dump(mode="json")


def _deployed_task(services: ApiServices) -> Task:
    """A task bound to a real app, workload, deployment, and container."""

    deployment = services.deployments.deploy(
        DeploymentSpec(name="reindex", handler="jobs.search:reindex")
    )
    stub = next(
        item
        for item in ControlPlaneService(services.context).list_stubs()
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
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    task = _deployed_task(isolated_services)
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = _headers(isolated_services, "task-row-reader", scopes=["read"])

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
