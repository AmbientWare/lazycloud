from __future__ import annotations

import importlib
import subprocess
import sys
import types
from pathlib import Path

import pytest
from lazycloud.references import (
    HandlerReferenceError,
    dotted_reference,
    source_root_handler_reference,
)


def _module_function() -> int:
    return 1


def test_dotted_reference_uses_importable_module_name() -> None:
    reference = dotted_reference(_module_function)

    assert reference == f"{_module_function.__module__}:_module_function"
    assert ":" in reference


def test_dotted_reference_derives_main_module_from_script_file(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    script = package / "entry.py"
    script.write_text(
        """
from lazycloud.references import dotted_reference


def fn():
    return 1


if __name__ == "__main__":
    print(dotted_reference(fn))
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "pkg/entry.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "pkg.entry:fn"


def test_dotted_reference_rejects_fileless_main_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = types.ModuleType("__main__")
    exec("def fn():\n    return 1\n", module.__dict__)
    monkeypatch.setitem(sys.modules, "__main__", module)

    with pytest.raises(HandlerReferenceError, match="without a source file"):
        dotted_reference(module.fn)


def test_dotted_reference_rejects_main_script_outside_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    script = outside / "entry.py"
    script.write_text("def fn():\n    return 1\n", encoding="utf-8")
    module = types.ModuleType("__main__")
    module.__file__ = str(script)
    exec(script.read_text(encoding="utf-8"), module.__dict__)
    monkeypatch.setitem(sys.modules, "__main__", module)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    with pytest.raises(HandlerReferenceError, match="outside the current directory"):
        dotted_reference(module.fn)


def test_source_root_reference_preserves_implicit_namespace_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "e2e" / "checkpoints"
    source_root.mkdir(parents=True)
    module_path = source_root / "checkpoint_source_root_test.py"
    module_path.write_text("def endpoint():\n    return 'ok'\n", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    module = importlib.import_module("e2e.checkpoints.checkpoint_source_root_test")
    try:
        reference = source_root_handler_reference(
            f"{module.__name__}:endpoint",
            source_root,
        )
    finally:
        sys.modules.pop(module.__name__, None)

    assert reference.handler == "e2e.checkpoints.checkpoint_source_root_test:endpoint"
    assert reference.root == source_root.resolve()
    assert reference.archive_prefix == ("e2e", "checkpoints")


def test_source_root_reference_has_no_prefix_for_normal_import_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    module_path = package / "handler.py"
    module_path.write_text("def endpoint():\n    return 'ok'\n", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    module = importlib.import_module("package.handler")
    try:
        reference = source_root_handler_reference(
            f"{module.__name__}:endpoint",
            tmp_path,
        )
    finally:
        sys.modules.pop(module.__name__, None)
        sys.modules.pop("package", None)

    assert reference.handler == "package.handler:endpoint"
    assert reference.root == tmp_path.resolve()
    assert reference.archive_prefix == ()


def test_source_root_reference_rejects_loaded_handler_outside_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "bundle"
    source_root.mkdir()
    module_path = tmp_path / "outside_source_root_test.py"
    module_path.write_text("def endpoint():\n    return 'ok'\n", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    module = importlib.import_module("outside_source_root_test")
    try:
        with pytest.raises(HandlerReferenceError, match="outside deployment source root"):
            source_root_handler_reference(f"{module.__name__}:endpoint", source_root)
    finally:
        sys.modules.pop(module.__name__, None)
