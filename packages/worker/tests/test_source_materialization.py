from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from shared.container_requests import WORKER_USER_CODE_VOLUME, RequestMount
from worker.events import ContainerRequestContext
from worker.source_code import SourceCodePackageMaterializer


def test_worker_source_materializer_extracts_isolated_container_workspaces(tmp_path: Path) -> None:
    data = _zip_bytes({"main.py": "print('hello')\n", "pkg/__init__.py": ""})
    digest = hashlib.sha256(data).hexdigest()
    archive_path = tmp_path / "source.zip"
    archive_path.write_bytes(data)
    materializer = SourceCodePackageMaterializer(
        cache_root=tmp_path / "cache",
        workspace_root=tmp_path / "workspaces",
    )
    first = materializer.materialize(
        ContainerRequestContext(
            container_id="source-sdk1-a",
            workspace_id="workspace-team",
            workspace_name="team",
        ),
        _mount(digest, archive_path),
    )
    Path(first.workspace_path, "main.py").write_text("mutated\n", encoding="utf-8")

    second = materializer.materialize(
        ContainerRequestContext(
            container_id="source-sdk1-b",
            workspace_id="workspace-team",
            workspace_name="team-renamed",
        ),
        _mount(digest, archive_path),
    )

    assert first.workspace_path != second.workspace_path
    assert Path(first.cache_path, "main.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert Path(second.workspace_path, "main.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert second.cache_hit


def test_worker_source_materializer_purges_only_requested_workspace_objects(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    materializer = SourceCodePackageMaterializer(
        cache_root=cache_root, workspace_root=tmp_path / "workspaces"
    )
    data = _zip_bytes({"main.py": "print('retained')\n"})
    archive = tmp_path / "source.zip"
    archive.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    paths: list[Path] = []
    for workspace_id, object_id in (
        ("workspace-a", "object-1"),
        ("workspace-a", "object-2"),
        ("workspace-b", "object-1"),
    ):
        prepared = materializer.materialize(
            ContainerRequestContext(
                container_id=f"{workspace_id}-{object_id}", workspace_id=workspace_id
            ),
            RequestMount(
                local_path=str(archive),
                mount_path=WORKER_USER_CODE_VOLUME,
                source_object_id=object_id,
                source_sha256=digest,
            ),
        )
        paths.append(Path(prepared.cache_path))
    target, other_object, other_workspace = paths

    result = materializer.purge("workspace-a", ["object-1", "object-1"])

    assert result.workspace_id == "workspace-a"
    assert result.removed_paths == [str(target)]
    assert not target.exists()
    assert other_object.is_dir()
    assert other_workspace.is_dir()


def _mount(digest: str, archive_path: Path) -> RequestMount:
    return RequestMount(
        local_path=str(archive_path),
        mount_path=WORKER_USER_CODE_VOLUME,
        source_object_id="obj-source",
        source_sha256=digest,
    )


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return buffer.getvalue()
