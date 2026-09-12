from __future__ import annotations

from typing import Protocol, TypeGuard, runtime_checkable

from lazycloud.abstractions.app import App
from lazycloud.abstractions.endpoint import ASGI, Endpoint
from lazycloud.abstractions.function import Function
from lazycloud.abstractions.pod import Pod
from lazycloud.abstractions.sandbox import Sandbox
from lazycloud.cli.handler_workflows import (
    apply_handler_reference,
    load_handler_object,
)
from lazycloud.json_contracts import JsonValue

Workload = Function[..., JsonValue] | Endpoint[..., JsonValue] | ASGI | Pod | Sandbox
UserDeployable = App | Workload


@runtime_checkable
class JsonCallable(Protocol):
    def __call__(self, *args: JsonValue) -> JsonValue: ...


HandlerTarget = UserDeployable | JsonCallable | None


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
    if isinstance(target, JsonCallable):
        return target
    return None
