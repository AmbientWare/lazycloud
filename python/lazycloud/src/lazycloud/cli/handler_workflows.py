from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, runtime_checkable

import typer
from shared.env import importing_user_code

from lazycloud.abstractions.app import App
from lazycloud.abstractions.endpoint import ASGI, Endpoint
from lazycloud.abstractions.function import Function
from lazycloud.abstractions.pod import Pod
from lazycloud.cli.components.errors import ClientError

_MISSING = object()


class HandlerLoadError(ClientError):
    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(
            message,
            type="invalid_handler",
            title="Handler not loaded",
            hint=hint,
            exit_code=2,
        )


@runtime_checkable
class HandlerReferenceTarget(Protocol):
    def set_handler(self, reference: str) -> None: ...


def load_handler_object(reference: str) -> object:
    if ":" not in reference:
        msg = "handler reference must be formatted as 'module:object' or '/path/file.py:object'"
        raise HandlerLoadError(msg)
    module_ref, object_path = reference.split(":", 1)
    if not module_ref or not object_path:
        msg = "handler reference must include both module and object"
        raise HandlerLoadError(msg)
    module = _load_module(module_ref)
    target: object = module
    owner = module.__name__
    for part in object_path.split("."):
        found = getattr(target, part, _MISSING)
        if found is _MISSING:
            if target is module:
                raise HandlerLoadError(
                    f"module {owner} has no attribute {part!r}",
                    hint=_defined_handlers_hint(module_ref, module),
                )
            raise HandlerLoadError(f"{owner} has no attribute {part!r}")
        target = found
        owner = f"{owner}.{part}"
    return target


def apply_handler_reference(user_object: object, reference: str) -> object:
    module_ref, object_path = reference.split(":", 1)
    canonical = f"{_canonical_module_name(module_ref)}:{object_path}"
    if isinstance(user_object, HandlerReferenceTarget):
        user_object.set_handler(canonical)
    return user_object


def load_deployment_object(reference: str) -> object:
    if ":" in reference:
        return apply_handler_reference(load_handler_object(reference), reference)
    module = _load_module(reference)
    apps: list[tuple[str, App]] = []
    seen: set[int] = set()
    for name, value in vars(module).items():
        if not isinstance(value, App) or id(value) in seen:
            continue
        seen.add(id(value))
        apps.append((name, value))
    if not apps:
        raise HandlerLoadError(f"no App found in {reference}; use {reference}:handler")
    if len(apps) > 1:
        choices = ", ".join(f"{reference}:{name}" for name, _ in apps)
        raise HandlerLoadError(f"multiple apps found; select one: {choices}")
    return apps[0][1]


def invoke_handler_method(
    user_object: object,
    method_name: str,
    *,
    kwargs: dict[str, Any] | None = None,
) -> Any:
    method = getattr(user_object, method_name, None)
    if not callable(method):
        msg = f"handler does not support {method_name} workflow"
        raise typer.BadParameter(msg)
    selected_kwargs = _accepted_kwargs(method, kwargs or {})
    return method(**selected_kwargs)


def call_handler(
    user_object: object,
    *,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> Any:
    if not callable(user_object):
        msg = "handler is not callable"
        raise typer.BadParameter(msg)
    return user_object(*(args or []), **(kwargs or {}))


def _defined_handlers_hint(module_ref: str, module: ModuleType) -> str:
    names = sorted(
        name
        for name, value in vars(module).items()
        if isinstance(value, (App, Function, Endpoint, ASGI, Pod))
    )
    if not names:
        return f"{module_ref} defines no app or workload."
    return f"Apps and workloads in {module_ref}: " + ", ".join(
        f"{module_ref}:{name}" for name in names
    )


def _load_module(module_ref: str) -> ModuleType:
    module_name = _canonical_module_name(module_ref)
    _ensure_current_directory_on_path()
    with importing_user_code():
        return importlib.import_module(module_name)


def _canonical_module_name(module_ref: str) -> str:
    path = Path(module_ref).expanduser()
    if not module_ref.endswith(".py") and not path.exists():
        return module_ref
    return _module_name_for_path(path)


def _module_name_for_path(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        msg = f"handler file not found: {path}"
        raise HandlerLoadError(msg)
    try:
        relative = resolved.relative_to(Path.cwd().resolve())
    except ValueError as exc:
        msg = (
            f"handler file {resolved} is outside the current directory; run the command "
            "from the project root containing it so the deployed source bundle can load "
            "the handler"
        )
        raise HandlerLoadError(msg) from exc
    parts = relative.with_suffix("").parts
    if not parts or not all(part.isidentifier() for part in parts):
        msg = (
            f"handler path {relative} is not importable as a Python module; "
            "every path segment must be a valid Python identifier"
        )
        raise HandlerLoadError(msg)
    return ".".join(parts)


def _accepted_kwargs(method: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return kwargs
    accepts_var_keyword = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if accepts_var_keyword:
        return kwargs
    return {key: value for key, value in kwargs.items() if key in signature.parameters}


def _ensure_current_directory_on_path() -> None:
    current_directory = str(Path.cwd())
    if current_directory not in sys.path:
        sys.path.insert(0, current_directory)


__all__ = [
    "HandlerLoadError",
    "HandlerReferenceTarget",
    "apply_handler_reference",
    "call_handler",
    "invoke_handler_method",
    "load_deployment_object",
    "load_handler_object",
]
