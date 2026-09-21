from __future__ import annotations

import importlib
import pickle
import sys
import zipfile
from pathlib import Path

import pytest
from lazycloud.references import source_root_handler_reference
from lazycloud.session.deployment import DeploymentClient
from lazycloud.source_sync import (
    SOURCE_IGNORE_FILE,
    SOURCE_PACKAGE_BUCKET,
    SOURCE_PACKAGE_CONTENT_TYPE,
    SourcePackageSyncer,
    SourcePackageSyncError,
    build_source_package_archive,
    collect_source_files,
)
from lazycloud.values import cloudpickle_bytes
from shared.deployment_records import DeploymentSpec
from tests.fakes import FakeDeploymentClient, FakeUploadClient

pytestmark = pytest.mark.usefixtures("isolated_imports")


def test_source_package_sync_excludes_ignored_files(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("SECRET=hidden\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "__pycache__" / "app.pyc").write_bytes(b"hidden")
    (tmp_path / ".lazycloud").mkdir()
    (tmp_path / ".lazycloud" / "provider-acceptance.tar").write_bytes(b"hidden")
    (tmp_path / "apps" / "web" / "test-results").mkdir(parents=True)
    (tmp_path / "apps" / "web" / "test-results" / ".last-run.json").write_text(
        '{"status":"passed"}\n',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "web" / "playwright-report").mkdir()
    (tmp_path / "apps" / "web" / "playwright-report" / "index.html").write_text(
        "generated\n",
        encoding="utf-8",
    )

    client = FakeUploadClient(object_id="obj-source")
    syncer = SourcePackageSyncer(client, root_dir=tmp_path)

    first = syncer.sync()
    assert first.object_id == "obj-source"
    assert len(client.uploads) == 1
    upload = client.uploads[0]
    assert upload["bucket"] == SOURCE_PACKAGE_BUCKET
    assert upload["content_type"] == SOURCE_PACKAGE_CONTENT_TYPE
    assert str(upload["name"]).startswith("sources/")
    data = upload["data"]
    assert isinstance(data, bytes)
    zip_path = tmp_path / "source.zip"
    zip_path.write_bytes(data)
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == ["app.py", "pkg/__init__.py"]
        assert archive.read("app.py") == b"print('hello')\n"


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


def test_deployment_prepare_writes_ignore_file_once(tmp_path: Path) -> None:
    (tmp_path / "handler.py").write_text("def handle(): return 'ok'\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("", encoding="utf-8")
    client = DeploymentClient(
        client=FakeDeploymentClient(),
        object_client=FakeUploadClient(object_id="obj"),
        sync_source=True,
        source_root=tmp_path,
    )
    spec = DeploymentSpec(name="demo", handler="handler:handle")

    client.prepare(spec, workspace="team")
    ignore_file = tmp_path / SOURCE_IGNORE_FILE
    written = ignore_file.read_text(encoding="utf-8")
    assert written.startswith("# Written by the LazyCloud SDK.")
    assert ".venv\n" in written

    ignore_file.write_text("custom/\n", encoding="utf-8")
    client.prepare(spec, workspace="team")

    assert ignore_file.read_text(encoding="utf-8") == "custom/\n"


def test_source_package_sync_preserves_canonical_module_prefix(tmp_path: Path) -> None:
    (tmp_path / "workloads.py").write_text("class OpaqueNumber:\n    pass\n", encoding="utf-8")
    client = FakeUploadClient(object_id="obj-prefixed-source")

    result = SourcePackageSyncer(
        client,
        root_dir=tmp_path,
        archive_prefix=("tests", "e2e", "local", "function"),
    ).sync()

    assert result.files == ("tests/e2e/local/function/workloads.py",)
    data = client.uploads[0]["data"]
    assert isinstance(data, bytes)
    zip_path = tmp_path / "prefixed-source.zip"
    zip_path.write_bytes(data)
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == ["tests/e2e/local/function/workloads.py"]


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


def test_deployment_prepare_syncs_source_and_sends_object_id(tmp_path: Path) -> None:
    (tmp_path / "handler.py").write_text("def handle(): return 'ok'\n", encoding="utf-8")
    gateway = FakeDeploymentClient(stub_id="stub-source")
    upload_client = FakeUploadClient(object_id="obj-runtime-source")

    response = DeploymentClient(
        client=gateway,
        object_client=upload_client,
        sync_source=True,
        source_root=tmp_path,
    ).prepare(DeploymentSpec(name="demo", handler="handler:handle"), workspace="team")

    assert response.stub_id == "stub-source"
    assert gateway.requests[0].object_id == "obj-runtime-source"
    assert gateway.requests[0].handler == "handler:handle"
    assert gateway.requests[0].workspace == "team"
    assert upload_client.uploads[0]["bucket"] == SOURCE_PACKAGE_BUCKET


def test_deployment_prepare_keeps_handler_module_and_prefixes_nested_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    source_root = project / "project_namespace" / "functions"
    source_root.mkdir(parents=True)
    (source_root / "handler.py").write_text("def handle(): return 'ok'\n", encoding="utf-8")
    module_name = "project_namespace.functions.handler"
    monkeypatch.setattr(sys, "path", [str(project), *sys.path])
    importlib.import_module(module_name)
    gateway = FakeDeploymentClient(stub_id="stub-source")
    upload_client = FakeUploadClient(object_id="obj-runtime-source")

    DeploymentClient(
        client=gateway,
        object_client=upload_client,
        sync_source=True,
        source_root=source_root,
    ).prepare(
        DeploymentSpec(name="demo", handler=f"{module_name}:handle"),
        workspace="team",
    )

    assert gateway.requests[0].handler == f"{module_name}:handle"
    data = upload_client.uploads[0]["data"]
    assert isinstance(data, bytes)
    zip_path = tmp_path / "deployment-source.zip"
    zip_path.write_bytes(data)
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == ["project_namespace/functions/handler.py"]
    _clear_test_module(module_name)
