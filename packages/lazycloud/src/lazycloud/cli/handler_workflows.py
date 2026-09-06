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

from lazycloud.abstractions.endpoint import ASGI, Endpoint
from lazycloud.abstractions.function import Function


class HandlerLoadError(ValueError):
    pass


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
    for part in object_path.split("."):
        target = getattr(target, part)
    return target


def apply_handler_reference(user_object: object, reference: str) -> object:
    module_ref, object_path = reference.split(":", 1)
    canonical = f"{_canonical_module_name(module_ref)}:{object_path}"
    if isinstance(user_object, HandlerReferenceTarget):
        user_object.set_handler(canonical)
    return user_object


def load_deployment_objects(reference: str) -> tuple[tuple[str, object], ...]:
    if ":" in reference:
        return ((reference, apply_handler_reference(load_handler_object(reference), reference)),)
    module = _load_module(reference)
    targets: list[tuple[str, object]] = []
    seen: set[int] = set()
    for name, value in vars(module).items():
        if _decorated_function_module(value) != module.__name__ or id(value) in seen:
            continue
        seen.add(id(value))
        handler = f"{module.__name__}:{name}"
        targets.append((handler, apply_handler_reference(value, handler)))
    if not targets:
        raise HandlerLoadError(f"no deployable decorated functions found in {reference}")
    return tuple(targets)


def _decorated_function_module(value: object) -> str | None:
    if isinstance(value, (Function, Endpoint, ASGI)):
        return value.__module__
    return None


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
    "load_deployment_objects",
    "load_handler_object",
]
