from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.records.apps import StubRecord
from database.repositories.execution import TaskDependencyRepository
from execution.task_rerun import TaskRerunService
from pydantic import JsonValue
from shared.deployments import StubKind
from shared.errors import InvalidInputError, NotFoundError
from shared.function_payloads import FunctionCloudpickleInvocation, FunctionJsonInvocation
from shared.http.functions import (
    FUNCTION_CALL_REF_MARKER,
    FunctionInvokeBody,
    FunctionInvokeResponse,
)
from shared.tasks import Task, TaskDependency, TaskStatus


@dataclass
class _RecordingInvoker:
    services: ApiServices
    workspace_id: str
    bodies: list[FunctionInvokeBody] = field(default_factory=list)

    def function_invoke(self, request: FunctionInvokeBody) -> FunctionInvokeResponse:
        self.bodies.append(request)
        created = self.services.tasks.create(
            "function-rerun",
            workspace_id=self.workspace_id,
            stub_id=request.stub_id,
        )
        return FunctionInvokeResponse.from_result(task_id=created.id)


@dataclass
class _RerunFixture:
    service: TaskRerunService
    invoker: _RecordingInvoker


def _rerun_service(isolated_services: ApiServices, workspace_id: str) -> _RerunFixture:
    invoker = _RecordingInvoker(isolated_services, workspace_id)
    service = TaskRerunService(isolated_services, function_invoker=invoker)
    return _RerunFixture(service=service, invoker=invoker)


def _finished_function_task(
    isolated_services: ApiServices,
    stub: StubRecord,
    *,
    args: list[JsonValue],
    kwargs: dict[str, JsonValue],
    status: TaskStatus = TaskStatus.Failed,
    invocation: FunctionCloudpickleInvocation | FunctionJsonInvocation | None = None,
) -> Task:
    task = isolated_services.tasks.create(
        f"function-{stub.name}",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        invocation=invocation or FunctionJsonInvocation(args=args, kwargs=kwargs),
    )
    return isolated_services.tasks.transition(task, status)


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
    fixture = _rerun_service(isolated_services, stub.workspace_id)

    new_task = fixture.service.rerun(workspace_id=stub.workspace_id, task_id=source.id)

    assert new_task.id != source.id
    assert len(fixture.invoker.bodies) == 1
    body = fixture.invoker.bodies[0]
    assert body.stub_id == stub.id
    assert body.headless is True
    assert body.invocation == invocation
    assert isinstance(body.invocation, FunctionCloudpickleInvocation)
    assert body.invocation.bytes_value() == original_body


def test_rerun_copies_declared_dependency_edges(isolated_services: ApiServices) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("rerun-dependency", kind=StubKind.Function)
    upstream = _finished_function_task(
        isolated_services,
        stub,
        args=[1],
        kwargs={},
        status=TaskStatus.Complete,
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
    fixture = _rerun_service(isolated_services, stub.workspace_id)

    fixture.service.rerun(workspace_id=stub.workspace_id, task_id=source.id)

    assert [dependency.task_id for dependency in fixture.invoker.bodies[0].dependencies] == [
        upstream.id
    ]


def test_rerun_rejects_task_queue_tasks(isolated_services: ApiServices) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("rerun-queue", kind=StubKind.TaskQueue)
    task = isolated_services.tasks.create(
        f"taskqueue-{stub.id}",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
    )
    source = isolated_services.tasks.transition(task, TaskStatus.Complete)
    fixture = _rerun_service(isolated_services, stub.workspace_id)

    with pytest.raises(InvalidInputError):
        fixture.service.rerun(workspace_id=stub.workspace_id, task_id=source.id)


def test_rerun_rejects_live_tasks(isolated_services: ApiServices) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("rerun-live", kind=StubKind.Function)
    task = isolated_services.tasks.create(
        "function-live",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
    )
    fixture = _rerun_service(isolated_services, stub.workspace_id)

    with pytest.raises(InvalidInputError):
        fixture.service.rerun(workspace_id=stub.workspace_id, task_id=task.id)


def test_rerun_scopes_to_workspace(isolated_services: ApiServices) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("rerun-scope", kind=StubKind.Function)
    source = _finished_function_task(isolated_services, stub, args=[], kwargs={})
    fixture = _rerun_service(isolated_services, stub.workspace_id)

    with pytest.raises(NotFoundError):
        fixture.service.rerun(workspace_id="not-the-workspace", task_id=source.id)
