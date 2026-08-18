"""Re-submit a finished function task through canonical isolated execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from control.service import ControlPlaneService, StubKind
from database.repositories.execution import TaskDependencyRepository
from shared.errors import InvalidInputError, NotFoundError, UpstreamUnavailableError
from shared.http.functions import (
    FunctionCallDependency,
    FunctionInvokeBody,
    FunctionInvokeResponse,
)
from shared.tasks import Task, is_terminal_task_status

from execution.services import ExecutionServices


class FunctionInvoker(Protocol):
    def function_invoke(self, request: FunctionInvokeBody) -> FunctionInvokeResponse: ...


@dataclass(slots=True)
class TaskRerunService:
    services: ExecutionServices
    function_invoker: FunctionInvoker
    control_plane: ControlPlaneService = field(init=False)

    def __post_init__(self) -> None:
        self.control_plane = ControlPlaneService(self.services.context)

    def rerun(self, *, workspace_id: str, task_id: str) -> Task:
        source = self._workspace_task(workspace_id, task_id)
        if not is_terminal_task_status(source.status):
            msg = "task is still running; cancel it before re-running"
            raise InvalidInputError(msg)
        if source.stub_id:
            return self._rerun_stub_task(source)
        if source.command:
            msg = "historic command tasks cannot be re-run"
            raise InvalidInputError(msg)
        msg = "task has no stub or command to re-run"
        raise InvalidInputError(msg)

    def _rerun_stub_task(self, source: Task) -> Task:
        try:
            stub = self.control_plane.get_stub(source.stub_id or "")
        except NotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        if stub.kind is StubKind.Function:
            return self._rerun_function_task(stub.id, source)
        msg = f"tasks of kind {stub.kind.value} cannot be re-run"
        raise InvalidInputError(msg)

    def _rerun_function_task(self, stub_id: str, source: Task) -> Task:
        if source.invocation is None:
            raise InvalidInputError("function task has no durable invocation to re-run")
        with self.services.context.database.session() as session:
            dependencies = TaskDependencyRepository(session).list_for_task(source.id)
        response = self.function_invoker.function_invoke(
            FunctionInvokeBody(
                stub_id=stub_id,
                headless=True,
                invocation=source.invocation,
                dependencies=[
                    FunctionCallDependency(
                        task_id=dependency.upstream_task_id,
                        workspace_id=dependency.workspace_id or "",
                        edge_type=dependency.edge_type,
                    )
                    for dependency in dependencies
                ],
            )
        )
        if not response.task_id:
            raise UpstreamUnavailableError(response.output or "failed to re-run task")
        return self.services.tasks.get(response.task_id)

    def _workspace_task(self, workspace_id: str, task_id: str) -> Task:
        try:
            task = self.services.tasks.get(task_id)
        except NotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        if task.workspace_id != workspace_id:
            msg = f"task not found: {task_id}"
            raise NotFoundError(msg)
        return task


__all__ = ["FunctionInvoker", "TaskRerunService"]
