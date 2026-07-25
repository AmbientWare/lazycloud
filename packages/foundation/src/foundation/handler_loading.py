from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from shared.env import importing_user_code

USER_CODE_DIR = Path("/mnt/code")


def load_callable(reference: str) -> Callable[..., Any]:
    user_code_root = configure_user_code_path()
    if ":" not in reference:
        msg = "handler reference must be formatted as 'module:function' or '/path/file.py:function'"
        raise ValueError(msg)
    module_ref, function_name = reference.split(":", 1)
    if not function_name:
        msg = "handler reference is missing a function name"
        raise ValueError(msg)
    if module_ref.endswith(".py") or Path(module_ref).exists():
        module = _load_module_from_path(Path(module_ref).expanduser().resolve())
    else:
        _evict_shadowed_user_modules(module_ref, user_code_root)
        with importing_user_code():
            module = importlib.import_module(module_ref)
    target: Any = module
    for part in function_name.split("."):
        target = getattr(target, part)
    if not callable(target):
        msg = f"handler is not callable: {reference}"
        raise TypeError(msg)
    return target


def configure_user_code_path(root: Path | None = None) -> Path:
    root = root or USER_CODE_DIR
    candidates = [
        root,
        root / "src",
        *(path / "src" for path in sorted((root / "packages").glob("*"))),
    ]
    for path in reversed(candidates):
        if path.exists():
            value = str(path)
            if value not in sys.path:
                sys.path.insert(0, value)
    importlib.invalidate_caches()
    return root


def evict_user_code_modules(root: Path | None = None) -> None:
    user_code_root = configure_user_code_path(root).resolve()
    for module_name, module in list(sys.modules.items()):
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            continue
        try:
            Path(module_file).resolve().relative_to(user_code_root)
        except ValueError:
            continue
        sys.modules.pop(module_name, None)
    importlib.invalidate_caches()


def _evict_shadowed_user_modules(module_ref: str, root: Path) -> None:
    root = root.resolve()
    parts = module_ref.split(".")
    for index in range(1, len(parts) + 1):
        module_name = ".".join(parts[:index])
        module = sys.modules.get(module_name)
        if module is None:
            continue
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            continue
        try:
            Path(module_file).resolve().relative_to(root)
        except ValueError:
            del sys.modules[module_name]


def _load_module_from_path(path: Path) -> ModuleType:
    module_name = f"_handler_{path.stem}_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        msg = f"could not load module from {path}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    with importing_user_code():
        spec.loader.exec_module(module)
    return module


__all__ = [
    "USER_CODE_DIR",
    "configure_user_code_path",
    "evict_user_code_modules",
    "load_callable",
]
