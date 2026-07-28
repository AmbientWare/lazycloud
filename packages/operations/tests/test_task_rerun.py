from __future__ import annotations

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.records.apps import StubRecord
from database.repositories.execution import TaskDependencyRepository
from pydantic import JsonValue
from shared.deployments import StubKind
from shared.errors import InvalidInputError, NotFoundError
from shared.function_payloads import (
    FunctionCloudpickleInvocation,
    FunctionJsonInvocation,
    FunctionJsonResult,
)
from shared.http.functions import FUNCTION_CALL_REF_MARKER
from shared.tasks import Task, TaskDependency, TaskStatus


def _finished_function_task(
    isolated_services: ApiServices,
    stub: StubRecord,
    *,
    args: list[JsonValue],
    kwargs: dict[str, JsonValue],
    status: TaskStatus = TaskStatus.Failed,
    invocation: FunctionCloudpickleInvocation | FunctionJsonInvocation | None = None,
    function_result: FunctionJsonResult | None = None,
) -> Task:
    task = isolated_services.tasks.create(
        f"function-{stub.name}",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        invocation=invocation or FunctionJsonInvocation(args=args, kwargs=kwargs),
    )
    return isolated_services.tasks.transition(task, status, function_result=function_result)


def test_rerun_preserves_original_opaque_function_invocation(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("rerun-fn", kind=StubKind.Function)
    original_body = b"opaque-python-invocation"
    invocation = FunctionCloudpickleInvocation.from_bytes(original_body)
    source = _finished_function_task(
        isolated_services,
        stub,
        args=[],
        kwargs={},
        invocation=invocation,
    )

    new_task = isolated_services.task_rerun_service.rerun(
        workspace_id=stub.workspace_id,
        task_id=source.id,
    )

    assert new_task.id != source.id
    replayed = isolated_services.tasks.get(new_task.id).invocation
    assert replayed == invocation
    assert isinstance(replayed, FunctionCloudpickleInvocation)
    assert replayed.bytes_value() == original_body


def test_rerun_copies_declared_dependency_edges(isolated_services: ApiServices) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("rerun-dependency", kind=StubKind.Function)
    upstream = _finished_function_task(
        isolated_services,
        stub,
        args=[1],
        kwargs={},
        status=TaskStatus.Complete,
        function_result=FunctionJsonResult(value=1),
    )
    source = _finished_function_task(
        isolated_services,
        stub,
        args=[{FUNCTION_CALL_REF_MARKER: True, "task_id": upstream.id}],
        kwargs={},
        status=TaskStatus.Failed,
    )
    with isolated_services.context.database.session() as session:
        TaskDependencyRepository(session).create(
            TaskDependency(
                workspace_id=stub.workspace_id,
                task_id=source.id,
                upstream_task_id=upstream.id,
            )
        )

    new_task = isolated_services.task_rerun_service.rerun(
        workspace_id=stub.workspace_id,
        task_id=source.id,
    )

    with isolated_services.context.database.session() as session:
        edges = TaskDependencyRepository(session).list_for_task(new_task.id)
    assert [edge.upstream_task_id for edge in edges] == [upstream.id]


def test_rerun_rejects_task_queue_tasks(isolated_services: ApiServices) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("rerun-queue", kind=StubKind.TaskQueue)
    task = isolated_services.tasks.create(
        f"taskqueue-{stub.id}",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
    )
    source = isolated_services.tasks.transition(task, TaskStatus.Complete)

    with pytest.raises(InvalidInputError):
        isolated_services.task_rerun_service.rerun(
            workspace_id=stub.workspace_id,
            task_id=source.id,
        )


def test_rerun_rejects_live_tasks(isolated_services: ApiServices) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("rerun-live", kind=StubKind.Function)
    task = isolated_services.tasks.create(
        "function-live",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
    )

    with pytest.raises(InvalidInputError):
        isolated_services.task_rerun_service.rerun(
            workspace_id=stub.workspace_id,
            task_id=task.id,
        )


def test_rerun_scopes_to_workspace(isolated_services: ApiServices) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("rerun-scope", kind=StubKind.Function)
    source = _finished_function_task(isolated_services, stub, args=[], kwargs={})

    with pytest.raises(NotFoundError):
        isolated_services.task_rerun_service.rerun(
            workspace_id="not-the-workspace",
            task_id=source.id,
        )
