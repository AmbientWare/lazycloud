from __future__ import annotations

import importlib
import pickle
import sys
import zipfile
from pathlib import Path

import pytest
from lazycloud.references import source_root_handler_reference
from lazycloud.session.deployment import DeploymentClient
from lazycloud.session.source_sync import (
    SOURCE_PACKAGE_BUCKET,
    SOURCE_PACKAGE_CONTENT_TYPE,
    SourcePackageSyncer,
    SourcePackageSyncError,
    build_source_package_archive,
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

    unprefixed = build_source_package_archive(tmp_path)
    prefixed = build_source_package_archive(tmp_path, archive_prefix=("package",))

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
    archive = build_source_package_archive(
        source_root,
        archive_prefix=reference.archive_prefix,
    )
    runner_root = tmp_path / "runner"
    runner_root.mkdir()
    archive_path = tmp_path / "source.zip"
    archive_path.write_bytes(archive.data)
    with zipfile.ZipFile(archive_path) as source_archive:
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

    with pytest.raises(SourcePackageSyncError, match="importable module names"):
        build_source_package_archive(tmp_path, archive_prefix=archive_prefix)


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
