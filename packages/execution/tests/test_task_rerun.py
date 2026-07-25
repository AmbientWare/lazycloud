from __future__ import annotations

import pytest
from api.server.services import ApiServices
from execution.task_rerun import TaskRerunService
from shared.errors import InvalidInputError
from shared.http.functions import FunctionInvokeBody, FunctionInvokeResponse
from shared.tasks import TaskStatus


class _UnexpectedFunctionInvoker:
    def function_invoke(self, request: FunctionInvokeBody) -> FunctionInvokeResponse:
        raise AssertionError(f"historic command rerun invoked a function: {request}")


def test_historic_command_task_cannot_be_rerun(isolated_services: ApiServices) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    source = isolated_services.tasks.create(
        "historic-command",
        workspace_id=workspace_id,
        command=["python", "-c", "print('unsafe')"],
    )
    source = isolated_services.tasks.transition(source, TaskStatus.Complete, exit_code=0)
    initial_task_ids = {task.id for task in isolated_services.tasks.list()}

    service = TaskRerunService(
        isolated_services,
        function_invoker=_UnexpectedFunctionInvoker(),
    )

    with pytest.raises(InvalidInputError, match="historic command tasks cannot be re-run"):
        service.rerun(workspace_id=workspace_id, task_id=source.id)

    assert {task.id for task in isolated_services.tasks.list()} == initial_task_ids
