from __future__ import annotations

import io
import os
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import IO, Never

import pytest
from networking.internal_http import InternalHttpClient
from shared.container_requests import WORKER_USER_CODE_VOLUME, RequestMount
from worker.events import ContainerRequestContext
from worker.source_code import (
    SOURCE_CACHE_READY_FILE,
    WORKSPACE_READY_FILE,
    SourceCodeMaterializationError,
    SourceCodePackageMaterializer,
)


def test_source_materializer_cleans_only_the_terminal_container_paths(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    workspace_root = tmp_path / "workspaces"
    materializer = SourceCodePackageMaterializer(
        cache_root=cache_root,
        workspace_root=workspace_root,
    )
    container_id = "container-1"
    result = materializer.materialize(
        _request(container_id, "source-1"),
        _mount(tmp_path, "source-1", "print('ready')"),
    )
    failed_cache_temp = cache_root / f"failed.tmp.{container_id}"
    failed_cache_temp.mkdir(parents=True)
    (failed_cache_temp / "partial").write_text("partial", encoding="utf-8")
    other_cache_temp = cache_root / "failed.tmp.container-2"
    other_cache_temp.mkdir()

    cleanup = materializer.cleanup_container(container_id)

    assert cleanup.freed_bytes > 0
    assert not (workspace_root / container_id).exists()
    assert not failed_cache_temp.exists()
    assert other_cache_temp.exists()
    assert Path(result.cache_path).exists()


def test_source_materializer_prunes_only_inactive_owned_temporary_paths(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    workspace_root = tmp_path / "workspaces"
    materializer = SourceCodePackageMaterializer(
        cache_root=cache_root,
        workspace_root=workspace_root,
    )
    active_root = workspace_root / "active"
    inactive_root = workspace_root / "inactive"
    unrelated_root = workspace_root / "unrelated"
    for root in (active_root, inactive_root):
        (root / "workspace").mkdir(parents=True)
        (root / WORKSPACE_READY_FILE).write_text("ok", encoding="utf-8")
    unrelated_root.mkdir(parents=True)
    (unrelated_root / "workspace").mkdir()
    active_temp = cache_root / "source.tmp.active"
    inactive_temp = cache_root / "source.tmp.inactive"
    ready_cache = cache_root / "ready-source"
    for root in (active_temp, inactive_temp, ready_cache):
        root.mkdir(parents=True)
    (ready_cache / SOURCE_CACHE_READY_FILE).write_text("ok", encoding="utf-8")

    pruned = materializer.prune_abandoned_temporary_paths({"active"})

    assert pruned.active_container_count == 1
    assert active_root.exists()
    assert active_temp.exists()
    assert not inactive_root.exists()
    assert not inactive_temp.exists()
    assert unrelated_root.exists()
    assert ready_cache.exists()


def test_source_materializer_enforces_entry_and_byte_caps_after_copy(tmp_path: Path) -> None:
    entry_limited = SourceCodePackageMaterializer(
        cache_root=tmp_path / "entry-cache",
        workspace_root=tmp_path / "entry-workspaces",
        cache_max_entries=1,
    )
    first = entry_limited.materialize(
        _request("container-1", "source-1"),
        _mount(tmp_path, "source-1", "first"),
    )
    entry_limited.cleanup_container("container-1")
    os.utime(first.cache_path, ns=(1, 1))
    second = entry_limited.materialize(
        _request("container-2", "source-2"),
        _mount(tmp_path, "source-2", "second"),
    )

    assert not Path(first.cache_path).exists()
    assert Path(second.cache_path).exists()

    byte_limited = SourceCodePackageMaterializer(
        cache_root=tmp_path / "byte-cache",
        workspace_root=tmp_path / "byte-workspaces",
        cache_max_bytes=1,
    )
    oversized = byte_limited.materialize(
        _request("container-3", "source-3"),
        _mount(tmp_path, "source-3", "copied before eviction"),
    )

    assert (Path(oversized.workspace_path) / "main.py").read_text(encoding="utf-8") == (
        "copied before eviction"
    )
    assert not Path(oversized.cache_path).exists()


def test_source_materializer_rejects_container_path_traversal(tmp_path: Path) -> None:
    materializer = SourceCodePackageMaterializer(
        cache_root=tmp_path / "cache",
        workspace_root=tmp_path / "workspaces",
    )

    with pytest.raises(SourceCodeMaterializationError, match="single path segment"):
        materializer.cleanup_container("../outside")

    assert not (tmp_path / "outside").exists()


def test_source_download_error_never_discloses_capability_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "never-log-this-source-signature"

    def fail_request(
        self: InternalHttpClient,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterable[bytes] | IO[bytes] | None = None,
        timeout_seconds: float | None = None,
    ) -> Never:
        raise RuntimeError(f"failed request with {sentinel}")

    monkeypatch.setattr(InternalHttpClient, "request", fail_request)
    materializer = SourceCodePackageMaterializer(
        cache_root=tmp_path / "cache",
        workspace_root=tmp_path / "workspaces",
    )
    mount = RequestMount(
        mount_path=WORKER_USER_CODE_VOLUME,
        source_object_id="source-1",
        source_sha256="a" * 64,
        source_download_url=(f"https://objects.example.test/source.zip?X-Amz-Signature={sentinel}"),
    )

    with pytest.raises(SourceCodeMaterializationError) as caught:
        materializer.materialize(_request("container-1", "source-1"), mount)

    assert sentinel not in str(caught.value)
    assert caught.value.__cause__ is None


def _request(container_id: str, source_object_id: str) -> ContainerRequestContext:
    return ContainerRequestContext(
        container_id=container_id,
        workspace_id="workspace-1",
        mounts=[
            RequestMount(
                mount_path=WORKER_USER_CODE_VOLUME,
                source_object_id=source_object_id,
            )
        ],
    )


def _mount(tmp_path: Path, source_object_id: str, source: str) -> RequestMount:
    archive = tmp_path / f"{source_object_id}.zip"
    archive.write_bytes(_zip_bytes(source))
    return RequestMount(
        mount_path=WORKER_USER_CODE_VOLUME,
        source_object_id=source_object_id,
        local_path=str(archive),
    )


def _zip_bytes(source: str) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, mode="w") as archive:
        archive.writestr("main.py", source)
    return payload.getvalue()
