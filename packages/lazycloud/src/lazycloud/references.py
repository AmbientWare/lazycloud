from __future__ import annotations

import importlib.util
import inspect
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class HandlerReferenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SourceRootReference:
    handler: str
    root: Path
    archive_prefix: tuple[str, ...]


def dotted_reference(func: Callable[..., Any]) -> str:
    module_name = getattr(func, "__module__", "") or ""
    name = getattr(func, "__qualname__", getattr(func, "__name__", "callable"))
    if module_name not in {"", "__main__"}:
        return f"{module_name}:{name}"
    return f"{_module_name_from_source(func, name)}:{name}"


def source_root_handler_reference(
    reference: str,
    source_root: str | Path,
) -> SourceRootReference:
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        msg = f"deployment source root is not a directory: {root}"
        raise HandlerReferenceError(msg)
    module_name, separator, object_path = reference.partition(":")
    if not separator or not module_name or not object_path:
        raise HandlerReferenceError("handler reference must include both module and object")
    source = _handler_module_source(module_name, root)
    try:
        relative = source.relative_to(root)
    except ValueError as exc:
        msg = f"handler module {source} is outside deployment source root {root}"
        raise HandlerReferenceError(msg) from exc
    relative_module_parts = (
        relative.parent.parts if relative.name == "__init__.py" else relative.with_suffix("").parts
    )
    if relative_module_parts and not all(part.isidentifier() for part in relative_module_parts):
        msg = f"handler path {relative} is not importable from deployment source root {root}"
        raise HandlerReferenceError(msg)
    module_parts = tuple(module_name.split("."))
    if not module_parts or not all(part.isidentifier() for part in module_parts):
        raise HandlerReferenceError(f"handler module {module_name!r} is not importable")
    if (
        relative_module_parts
        and module_parts[-len(relative_module_parts) :] != relative_module_parts
    ):
        msg = (
            f"handler module {module_name!r} does not match its source path {relative}; "
            "load the handler through its canonical module name"
        )
        raise HandlerReferenceError(msg)
    archive_prefix = (
        module_parts if not relative_module_parts else module_parts[: -len(relative_module_parts)]
    )
    return SourceRootReference(
        handler=reference,
        root=root,
        archive_prefix=archive_prefix,
    )


def _handler_module_source(module_name: str, root: Path) -> Path:
    loaded = sys.modules.get(module_name)
    loaded_file = getattr(loaded, "__file__", None)
    if isinstance(loaded_file, str) and loaded_file:
        return Path(loaded_file).resolve()
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ModuleNotFoundError, ValueError):
        spec = None
    origin = spec.origin if spec is not None else None
    if isinstance(origin, str) and origin not in {"built-in", "frozen"}:
        return Path(origin).resolve()
    relative = Path(*module_name.split("."))
    candidates = (root / relative.with_suffix(".py"), root / relative / "__init__.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    msg = f"handler module {module_name!r} is not present in deployment source root {root}"
    raise HandlerReferenceError(msg)


def _module_name_from_source(func: Callable[..., Any], name: str) -> str:
    module = inspect.getmodule(func)
    source_file = getattr(module, "__file__", None)
    if not source_file:
        msg = (
            f"cannot build a deployable handler reference for {name!r}: it is defined "
            "in __main__ without a source file; move it into an importable module"
        )
        raise HandlerReferenceError(msg)
    resolved = Path(source_file).resolve()
    try:
        relative = resolved.relative_to(Path.cwd().resolve())
    except ValueError as exc:
        msg = (
            f"cannot build a deployable handler reference for {name!r}: its source file "
            f"{resolved} is outside the current directory, so the deployed source bundle "
            "cannot load it; run from the project root containing it"
        )
        raise HandlerReferenceError(msg) from exc
    parts = relative.with_suffix("").parts
    if not parts or not all(part.isidentifier() for part in parts):
        msg = (
            f"cannot build a deployable handler reference for {name!r}: source path "
            f"{relative} is not importable as a Python module"
        )
        raise HandlerReferenceError(msg)
    return ".".join(parts)
