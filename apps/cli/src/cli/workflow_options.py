from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeGuard, runtime_checkable

from lazycloud.abstractions.app import App
from lazycloud.abstractions.endpoint import ASGI, Endpoint
from lazycloud.abstractions.function import Function
from lazycloud.abstractions.pod import Pod
from lazycloud.abstractions.sandbox import Sandbox
from lazycloud.abstractions.shell import ShellSession
from lazycloud.cli.components.output import ModelDumpable
from lazycloud.cli.handler_workflows import (
    apply_handler_reference,
    load_handler_object,
)
from lazycloud.cli.workflow_options import WorkflowValue
from lazycloud.json_contracts import JsonValue

Workload = Function[..., JsonValue] | Endpoint[..., JsonValue] | ASGI | Pod | Sandbox
UserDeployable = App | Workload


@runtime_checkable
class JsonCallable(Protocol):
    def __call__(self, *args: JsonValue) -> JsonValue: ...


WorkflowResult = JsonValue | ModelDumpable


@runtime_checkable
class DeployWorkflow(Protocol):
    def deploy(self, **kwargs: WorkflowValue) -> WorkflowResult: ...


@runtime_checkable
class RunWorkflow(Protocol):
    def run(self, *args: JsonValue) -> WorkflowResult: ...


@runtime_checkable
class ShellWorkflow(Protocol):
    def shell(self, **kwargs: WorkflowValue) -> WorkflowResult | ShellSession: ...


@runtime_checkable
class ServeWorkflow(Protocol):
    def serve(self, **kwargs: WorkflowValue) -> WorkflowResult: ...


@dataclass(frozen=True, slots=True)
class GenericHandlerReference:
    reference: str


CustomWorkflow = DeployWorkflow | RunWorkflow | ShellWorkflow | ServeWorkflow
HandlerTarget = UserDeployable | JsonCallable | CustomWorkflow | GenericHandlerReference


def is_user_deployable(value: object) -> TypeGuard[UserDeployable]:
    return isinstance(value, (App, Function, Endpoint, ASGI, Pod, Sandbox))


def is_function_workload(value: HandlerTarget) -> TypeGuard[Function[..., JsonValue]]:
    return isinstance(value, Function)


def is_json_callable(value: HandlerTarget) -> TypeGuard[JsonCallable]:
    return isinstance(value, JsonCallable)


def load_cli_handler(reference: str) -> HandlerTarget:
    target = load_handler_object(reference)
    if is_user_deployable(target):
        apply_handler_reference(target, reference)
        return target
    if isinstance(
        target, (JsonCallable, DeployWorkflow, RunWorkflow, ShellWorkflow, ServeWorkflow)
    ):
        return target
    return GenericHandlerReference(reference)
