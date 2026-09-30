from __future__ import annotations

import importlib
import pickle
import sys
import zipfile
from pathlib import Path

import pytest
from lazycloud.references import source_root_handler_reference
from lazycloud.source_sync import (
    SOURCE_IGNORE_FILE,
    SourcePackageSyncError,
    build_source_package_archive,
    collect_source_files,
)
from lazycloud.values import cloudpickle_bytes

pytestmark = pytest.mark.usefixtures("isolated_imports")


def test_ignore_file_never_removes_the_baseline(tmp_path: Path) -> None:
    (tmp_path / SOURCE_IGNORE_FILE).write_text("data/\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "rows.csv").write_text("1\n", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "site.py").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / ".idea").mkdir()
    (tmp_path / ".idea" / "workspace.xml").write_text("", encoding="utf-8")

    files = [path.relative_to(tmp_path).as_posix() for path in collect_source_files(tmp_path)]

    assert sorted(files) == [".idea/workspace.xml", "app.py"]


def test_ignore_file_uses_gitignore_semantics(tmp_path: Path) -> None:
    (tmp_path / SOURCE_IGNORE_FILE).write_text(
        "build/\n**/generated/*.json\n*.log\n!keep.log\n",
        encoding="utf-8",
    )
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.txt").write_text("", encoding="utf-8")
    (tmp_path / "build.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "generated").mkdir(parents=True)
    (tmp_path / "pkg" / "generated" / "schema.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pkg" / "generated" / "schema.py").write_text("", encoding="utf-8")
    (tmp_path / "debug.log").write_text("", encoding="utf-8")
    (tmp_path / "keep.log").write_text("", encoding="utf-8")

    files = [path.relative_to(tmp_path).as_posix() for path in collect_source_files(tmp_path)]

    assert sorted(files) == ["build.py", "keep.log", "pkg/generated/schema.py"]


def test_source_archive_prefix_changes_digest(tmp_path: Path) -> None:
    (tmp_path / "workloads.py").write_text("def invoke(): return 1\n", encoding="utf-8")

    with (
        build_source_package_archive(tmp_path) as unprefixed,
        build_source_package_archive(tmp_path, archive_prefix=("package",)) as prefixed,
    ):
        assert unprefixed.sha256 != prefixed.sha256
        assert unprefixed.files == ("workloads.py",)
        assert prefixed.files == ("package/workloads.py",)


def test_prefixed_source_package_round_trips_custom_type_with_one_module_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    source_root = project / "custom_source_test" / "trusted_execution"
    source_root.mkdir(parents=True)
    (source_root / "workloads.py").write_text(
        """from dataclasses import dataclass

@dataclass(frozen=True)
class OpaqueNumber:
    value: int

def opaque_square(value: OpaqueNumber) -> OpaqueNumber:
    if not isinstance(value, OpaqueNumber):
        raise TypeError("custom type lost its module identity")
    return OpaqueNumber(value.value * value.value)
""",
        encoding="utf-8",
    )
    module_name = "custom_source_test.trusted_execution.workloads"
    monkeypatch.setattr(sys, "path", [str(project), *sys.path])
    client_module = importlib.import_module(module_name)
    reference = source_root_handler_reference(
        f"{module_name}:opaque_square",
        source_root,
    )
    invocation = cloudpickle_bytes({"args": (client_module.OpaqueNumber(9),), "kwargs": {}})
    runner_root = tmp_path / "runner"
    runner_root.mkdir()
    with (
        build_source_package_archive(
            source_root, archive_prefix=reference.archive_prefix
        ) as archive,
        zipfile.ZipFile(archive.path) as source_archive,
    ):
        source_archive.extractall(runner_root)

    _clear_test_module(module_name)
    monkeypatch.setattr(
        sys,
        "path",
        [str(runner_root), *[entry for entry in sys.path if entry != str(project)]],
    )
    runner_module = importlib.import_module(module_name)
    decoded = pickle.loads(invocation)
    value = decoded["args"][0]
    assert type(value) is runner_module.OpaqueNumber
    result_payload = cloudpickle_bytes(runner_module.opaque_square(value))

    _clear_test_module(module_name)
    monkeypatch.setattr(
        sys,
        "path",
        [str(project), *[entry for entry in sys.path if entry != str(runner_root)]],
    )
    restored_client_module = importlib.import_module(module_name)
    result = pickle.loads(result_payload)
    assert type(result) is restored_client_module.OpaqueNumber
    assert result == restored_client_module.OpaqueNumber(81)


@pytest.mark.parametrize(
    "archive_prefix",
    [("..",), ("/absolute",), ("not-a-module",), ("",)],
)
def test_source_archive_prefix_rejects_unsafe_paths(
    tmp_path: Path,
    archive_prefix: tuple[str, ...],
) -> None:
    (tmp_path / "workloads.py").write_text("def invoke(): return 1\n", encoding="utf-8")

    with (
        pytest.raises(SourcePackageSyncError, match="importable module names"),
        build_source_package_archive(tmp_path, archive_prefix=archive_prefix),
    ):
        pass


def _clear_test_module(module_name: str) -> None:
    parts = module_name.split(".")
    for length in range(len(parts), 0, -1):
        sys.modules.pop(".".join(parts[:length]), None)
