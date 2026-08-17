from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeGuard, runtime_checkable

from lazycloud.abstractions.app import App
from lazycloud.abstractions.endpoint import ASGI, Endpoint
from lazycloud.abstractions.function import Function
from lazycloud.abstractions.image import Image
from lazycloud.abstractions.pod import Pod
from lazycloud.abstractions.sandbox import Sandbox
from lazycloud.abstractions.shell import ShellSession
from lazycloud.cli.components.output import ModelDumpable
from lazycloud.cli.handler_workflows import (
    apply_handler_reference,
    load_handler_object,
)
from lazycloud.cli.workflow_options import DeploymentOverrides, WorkflowValue
from lazycloud.json_contracts import JsonValue, validate_json_object

Workload = Function[..., JsonValue] | Endpoint[..., JsonValue] | ASGI | Pod | Sandbox
MetadataWorkload = Function[..., JsonValue] | Endpoint[..., JsonValue] | Pod | Sandbox
UserDeployable = App | Workload
DeployableWorkload = Function[..., JsonValue] | Endpoint[..., JsonValue] | ASGI | Pod
ShellWorkload = Function[..., JsonValue] | Endpoint[..., JsonValue] | ASGI | Pod
ServeWorkload = Endpoint[..., JsonValue] | ASGI
ProgressWorkload = Function[..., JsonValue] | Endpoint[..., JsonValue] | ASGI


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


class KeywordWorkflow[ResultT](Protocol):
    def __call__(self, **kwargs: WorkflowValue) -> ResultT: ...


@dataclass(frozen=True, slots=True)
class GenericHandlerReference:
    reference: str


CustomWorkflow = DeployWorkflow | RunWorkflow | ShellWorkflow | ServeWorkflow
HandlerTarget = UserDeployable | JsonCallable | CustomWorkflow | GenericHandlerReference


def is_user_deployable(value: object) -> TypeGuard[UserDeployable]:
    return isinstance(value, (App, Function, Endpoint, ASGI, Pod, Sandbox))


def is_deployable_workload(value: HandlerTarget) -> TypeGuard[DeployableWorkload]:
    return isinstance(value, (Function, Endpoint, ASGI, Pod))


def is_function_workload(value: HandlerTarget) -> TypeGuard[Function[..., JsonValue]]:
    return isinstance(value, Function)


def is_pod_workload(value: HandlerTarget) -> TypeGuard[Pod]:
    return isinstance(value, Pod)


def is_shell_workload(value: HandlerTarget) -> TypeGuard[ShellWorkload]:
    return isinstance(value, (Function, Endpoint, ASGI, Pod))


def is_serve_workload(value: HandlerTarget) -> TypeGuard[ServeWorkload]:
    return isinstance(value, (Endpoint, ASGI))


def is_progress_workload(value: HandlerTarget) -> TypeGuard[ProgressWorkload]:
    return isinstance(value, (Function, Endpoint, ASGI))


def is_json_callable(value: HandlerTarget) -> TypeGuard[JsonCallable]:
    return isinstance(value, JsonCallable)


def is_deploy_workflow(value: object) -> TypeGuard[DeployWorkflow]:
    return isinstance(value, DeployWorkflow)


def is_run_workflow(value: object) -> TypeGuard[RunWorkflow]:
    return isinstance(value, RunWorkflow)


def is_shell_workflow(value: object) -> TypeGuard[ShellWorkflow]:
    return isinstance(value, ShellWorkflow)


def is_serve_workflow(value: object) -> TypeGuard[ServeWorkflow]:
    return isinstance(value, ServeWorkflow)


def invoke_keyword_workflow[ResultT](
    workflow: KeywordWorkflow[ResultT],
    values: dict[str, WorkflowValue],
) -> ResultT:
    try:
        signature = inspect.signature(workflow)
    except (TypeError, ValueError):
        return workflow(**values)
    accepts_all = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    selected = (
        values
        if accepts_all
        else {key: value for key, value in values.items() if key in signature.parameters}
    )
    return workflow(**selected)


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


def apply_deployment_overrides[DeployableT: UserDeployable](
    user_object: DeployableT,
    overrides: DeploymentOverrides,
) -> DeployableT:
    if not overrides.has_values():
        return user_object
    for target in _workload_targets(user_object):
        _apply_common_overrides(target, overrides)
        _apply_kind_overrides(target, overrides)
        if isinstance(target, (Function, Endpoint, Pod, Sandbox)):
            _apply_resource_metadata(target, overrides)
    return user_object


def _workload_targets(user_object: UserDeployable) -> list[Workload]:
    candidates = user_object.resources if isinstance(user_object, App) else (user_object,)
    return [
        candidate
        for candidate in candidates
        if isinstance(candidate, (Function, Endpoint, ASGI, Pod, Sandbox))
    ]


def _apply_common_overrides(target: Workload, overrides: DeploymentOverrides) -> None:
    if overrides.cpu is not None:
        target.cpu = overrides.cpu
    if overrides.memory is not None:
        target.memory = overrides.memory
    if overrides.gpu is not None:
        target.gpu = overrides.gpu
    if overrides.gpu_count is not None:
        target.gpu_count = overrides.gpu_count
    if overrides.pool is not None:
        target.pool = overrides.pool
    if overrides.dockerfile:
        target.image = Image.from_dockerfile(
            overrides.dockerfile,
            context_dir=overrides.context_dir,
        )
    elif overrides.image:
        target.image = Image.from_registry(overrides.image)
    if overrides.env:
        target.env = {**(target.env or {}), **overrides.env}
    if overrides.secrets:
        target.secrets = [*target.secrets, *overrides.secrets]


def _apply_kind_overrides(target: Workload, overrides: DeploymentOverrides) -> None:
    if overrides.keep_warm is not None:
        if isinstance(target, (Endpoint, Pod)):
            target.keep_warm = overrides.keep_warm
        elif isinstance(target, (ASGI, Sandbox)):
            target.keep_warm_seconds = overrides.keep_warm
    if isinstance(target, Pod):
        if overrides.tcp is not None:
            target.tcp = overrides.tcp
        if overrides.ports:
            target.ports = {**target.ports, **overrides.ports}
        if overrides.entrypoint:
            target.command = list(overrides.entrypoint)
    elif isinstance(target, Sandbox):
        if overrides.ports:
            target.ports = [*target.ports, *overrides.ports.values()]
        if overrides.entrypoint:
            target.command = list(overrides.entrypoint)
    if isinstance(target, (Endpoint, ASGI)) and overrides.sync_dir:
        target.sync_local_dir = str(Path(overrides.sync_dir))


def _apply_resource_metadata(
    target: MetadataWorkload,
    overrides: DeploymentOverrides,
) -> None:
    updates: dict[str, JsonValue] = {}
    if overrides.resource:
        updates["resource"] = overrides.resource
    if overrides.entrypoint:
        updates["entrypoint"] = list(overrides.entrypoint)
    if overrides.sync_dir:
        updates["sync_dir"] = str(Path(overrides.sync_dir))
    if not updates:
        return
    metadata = validate_json_object(target.metadata)
    target.metadata = {**metadata, **updates}
