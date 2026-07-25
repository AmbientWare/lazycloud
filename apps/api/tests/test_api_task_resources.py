from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.function_payloads import FunctionCloudpickleResult
from shared.http.errors import ErrorResponse
from shared.http.tasks import TaskResponse
from shared.identity import TokenKind
from shared.tasks import TaskStatus


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
    payload = TaskResponse.model_validate_json(response.content)
    assert payload.result == function_result.model_dump(mode="json")
