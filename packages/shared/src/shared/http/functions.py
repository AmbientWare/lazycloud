from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from shared.function_payloads import (
    FUNCTION_DEPENDENCY_MAX_COUNT,
    FunctionDependencyBinding,
    FunctionInvocationPayload,
    FunctionResultPayload,
    validate_function_dependency_bindings,
)
from shared.http.base import HttpModel
from shared.tasks import TaskStatus

FUNCTION_CALL_REF_MARKER = "__function_call_ref__"


class FunctionCallDependency(HttpModel):
    task_id: str
    workspace_id: str = ""
    edge_type: str = "argument"


class FunctionInvokeBody(HttpModel):
    stub_id: str
    invocation: FunctionInvocationPayload
    headless: bool = False
    parent_task_id: str = ""
    root_task_id: str = ""
    dependencies: list[FunctionCallDependency] = Field(
        default_factory=list,
        max_length=FUNCTION_DEPENDENCY_MAX_COUNT,
    )


class FunctionInvokeResponse(HttpModel):
    task_id: str = ""
    output: str = ""
    done: bool = False
    exit_code: int = 0
    result: FunctionResultPayload | None = None

    @classmethod
    def from_result(
        cls,
        *,
        task_id: str,
        result: FunctionResultPayload | None = None,
        output: str = "",
        done: bool = False,
        exit_code: int = 0,
    ) -> FunctionInvokeResponse:
        return cls(
            task_id=task_id,
            output=output,
            done=done,
            exit_code=exit_code,
            result=result,
        )


class FunctionGetArgsRequest(HttpModel):
    task_id: str
    container_id: str


class FunctionGetArgsResponse(HttpModel):
    invocation: FunctionInvocationPayload
    dependencies: list[FunctionDependencyBinding] = Field(
        default_factory=list,
        max_length=FUNCTION_DEPENDENCY_MAX_COUNT,
    )

    @model_validator(mode="after")
    def validate_bindings(self) -> FunctionGetArgsResponse:
        validate_function_dependency_bindings(self.dependencies)
        return self


class FunctionSetResultBody(HttpModel):
    task_id: str
    container_id: str
    result: FunctionResultPayload


class FunctionSetResultResponse(HttpModel):
    stored: bool = True
    status: TaskStatus = TaskStatus.Complete


class FunctionCallGraphNode(HttpModel):
    task_id: str
    parent_task_id: str = ""
    root_task_id: str = ""
    status: TaskStatus
    stub_id: str = ""
    deployment_id: str = ""
    app_id: str = ""
    name: str = ""
    function_name: str = ""
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    dependencies: list[str] = Field(default_factory=list)
    children: list[FunctionCallGraphNode] = Field(default_factory=list)


class FunctionCallGraphResponse(HttpModel):
    root_task_id: str = ""
    root: FunctionCallGraphNode | None = None
    nodes: list[FunctionCallGraphNode] = Field(default_factory=list)


class FunctionMonitorRequest(HttpModel):
    task_id: str
    stub_id: str
    container_id: str = ""


class FunctionMonitorResponse(HttpModel):
    cancelled: bool = False
    complete: bool = False
    timed_out: bool = False


class FunctionCronRequest(HttpModel):
    stub_id: str
    cron: str
    deployment_id: str


class FunctionCronResponse(HttpModel):
    cron_job_id: str = ""


__all__ = [
    "FUNCTION_CALL_REF_MARKER",
    "FunctionCallDependency",
    "FunctionCallGraphNode",
    "FunctionCallGraphResponse",
    "FunctionCronRequest",
    "FunctionCronResponse",
    "FunctionGetArgsRequest",
    "FunctionGetArgsResponse",
    "FunctionInvokeBody",
    "FunctionInvokeResponse",
    "FunctionMonitorRequest",
    "FunctionMonitorResponse",
    "FunctionSetResultBody",
    "FunctionSetResultResponse",
]
