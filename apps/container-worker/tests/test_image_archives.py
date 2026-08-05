from __future__ import annotations

import errno
import hashlib
import io
import shutil
import tarfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from container_worker_app.image_archives import (
    BrokeredImageArchiveSourceLoader,
    TarImageArchiveMounter,
)
from pydantic import JsonValue
from worker.container_checkpoints import TarContainerImageArchiver
from worker.container_startup import (
    IMAGE_MOUNT_MANIFEST_NAME,
    ImageMountManifest,
    WorkerImageMountRequest,
    WorkerImageMountStatus,
    WorkerImageSourceLoadRequest,
)
from worker.origin_access import CacheOriginCredentials
from worker.repository_client import WorkerRepositoryHttpClient
from worker.repository_payloads import GetCacheOriginCredentialsResponse


@contextmanager
def _serve_directory(root: Path) -> Iterator[str]:
    handler: Callable[..., SimpleHTTPRequestHandler] = partial(
        SimpleHTTPRequestHandler,
        directory=str(root),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_tar_image_archive_archiver_and_mounter_materialize_rootfs(tmp_path: Path) -> None:
    source_root = tmp_path / "rootfs"
    source_root.mkdir()
    (source_root / "app.py").write_text("print('ok')\n", encoding="utf-8")
    for index in range(130):
        (source_root / f"module_{index}.py").write_text("value = 1\n", encoding="utf-8")
    archive_root = tmp_path / "archives"
    progress: list[int] = []

    archived = TarContainerImageArchiver(archive_root).archive_image(
        source_root,
        "image-1",
        progress.append,
    )
    mounted = TarImageArchiveMounter().mount_image_archive(
        WorkerImageMountRequest(
            container_id="ctr-1",
            image_id="image-1",
            archive_path=str(archive_root / "image-1.rclip"),
            mount_point=str(tmp_path / "mounted"),
        )
    )

    assert archived.success
    assert progress == sorted(progress)
    assert len(progress) == len(set(progress))
    assert progress[-1] == 100
    assert mounted.mounted
    assert (tmp_path / "mounted" / "app.py").read_text(encoding="utf-8") == "print('ok')\n"


def test_tar_image_archive_mounter_preserves_image_root_symlinks(tmp_path: Path) -> None:
    archive_path = tmp_path / "image.rclip"
    with tarfile.open(archive_path, "w") as archive:
        executable = b"#!/bin/sh\n"
        file_info = tarfile.TarInfo("usr/bin/mawk")
        file_info.mode = 0o755
        file_info.size = len(executable)
        archive.addfile(file_info, io.BytesIO(executable))

        link_info = tarfile.TarInfo("etc/alternatives/awk")
        link_info.type = tarfile.SYMTYPE
        link_info.linkname = "/usr/bin/mawk"
        archive.addfile(link_info)

    mount_point = tmp_path / "mounted"
    mounted = TarImageArchiveMounter().mount_image_archive(
        WorkerImageMountRequest(
            container_id="ctr-1",
            image_id="image-1",
            archive_path=str(archive_path),
            mount_point=str(mount_point),
        )
    )

    link_path = mount_point / "etc/alternatives/awk"

    assert mounted.mounted
    assert link_path.is_symlink()
    assert str(link_path.readlink()) == "../../usr/bin/mawk"
    assert link_path.resolve() == (mount_point / "usr/bin/mawk").resolve()


def test_tar_image_archive_mounter_reuses_existing_mount_without_archive(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "image.rclip"
    with tarfile.open(archive_path, "w") as archive:
        data = b"cached\n"
        info = tarfile.TarInfo("app.py")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    mount_point = tmp_path / "mounted"
    first = TarImageArchiveMounter().mount_image_archive(
        WorkerImageMountRequest(
            container_id="ctr-1",
            image_id="image-1",
            archive_path=str(archive_path),
            mount_point=str(mount_point),
            repair_incomplete=True,
        )
    )
    archive_path.unlink()

    mounted = TarImageArchiveMounter().mount_image_archive(
        WorkerImageMountRequest(
            container_id="ctr-1",
            image_id="image-1",
            archive_path=str(archive_path),
            mount_point=str(mount_point),
        )
    )

    manifest = ImageMountManifest.model_validate_json(
        (mount_point / IMAGE_MOUNT_MANIFEST_NAME).read_text(encoding="utf-8")
    )

    assert first.mounted
    assert mounted.mounted
    assert mounted.reason == "complete image mount already exists"
    assert manifest.image_id == "image-1"
    assert manifest.archive_size_bytes > 0
    assert manifest.archive_entry_count == 1
    assert (mount_point / "app.py").read_text(encoding="utf-8") == "cached\n"


def test_tar_image_archive_mounter_reuses_concurrent_completed_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "image.rclip"
    with tarfile.open(archive_path, "w") as archive:
        data = b"concurrent\n"
        info = tarfile.TarInfo("app.py")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    mount_point = tmp_path / "mounted"
    original_replace = Path.replace

    def concurrent_replace(path: Path, target: Path) -> Path:
        if target == mount_point and path.name.endswith(".tmp"):
            shutil.copytree(path, mount_point)
            raise OSError(errno.ENOTEMPTY, "concurrent image mount installed")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", concurrent_replace)

    mounted = TarImageArchiveMounter().mount_image_archive(
        WorkerImageMountRequest(
            container_id="ctr-1",
            image_id="image-1",
            archive_path=str(archive_path),
            mount_point=str(mount_point),
        )
    )

    assert mounted.mounted
    assert mounted.reason == "complete image mount won concurrent materialization"
    assert (mount_point / "app.py").read_text(encoding="utf-8") == "concurrent\n"
    assert not list(tmp_path.glob(".mounted.*.tmp"))


def test_tar_image_archive_mounter_marks_incomplete_mount_for_repair(
    tmp_path: Path,
) -> None:
    mount_point = tmp_path / "mounted"
    mount_point.mkdir()
    (mount_point / "partial").write_text("stale", encoding="utf-8")

    result = TarImageArchiveMounter().mount_image_archive(
        WorkerImageMountRequest(
            container_id="ctr-1",
            image_id="image-1",
            archive_path=str(tmp_path / "missing.rclip"),
            mount_point=str(mount_point),
        )
    )

    assert result.status is WorkerImageMountStatus.RepairRequired
    assert (mount_point / "partial").read_text(encoding="utf-8") == "stale"


def test_tar_image_archive_mounter_rejects_manifest_for_changed_archive(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "image.rclip"
    with tarfile.open(archive_path, "w") as archive:
        data = b"complete\n"
        info = tarfile.TarInfo("workspace/app.py")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    mount_point = tmp_path / "mounted"
    first = TarImageArchiveMounter().mount_image_archive(
        WorkerImageMountRequest(
            container_id="ctr-1",
            image_id="image-1",
            archive_path=str(archive_path),
            mount_point=str(mount_point),
            repair_incomplete=True,
        )
    )
    with archive_path.open("ab") as archive:
        archive.write(b"changed")

    result = TarImageArchiveMounter().mount_image_archive(
        WorkerImageMountRequest(
            container_id="ctr-2",
            image_id="image-1",
            archive_path=str(archive_path),
            mount_point=str(mount_point),
        )
    )

    assert first.mounted
    assert result.status is WorkerImageMountStatus.RepairRequired


def test_tar_image_archive_mounter_atomically_repairs_incomplete_mount(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "image.rclip"
    with tarfile.open(archive_path, "w") as archive:
        data = b"complete\n"
        info = tarfile.TarInfo("workspace/app.py")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    mount_point = tmp_path / "mounted"
    mount_point.mkdir()
    (mount_point / "partial").write_text("stale", encoding="utf-8")

    result = TarImageArchiveMounter().mount_image_archive(
        WorkerImageMountRequest(
            container_id="ctr-1",
            image_id="image-1",
            archive_path=str(archive_path),
            mount_point=str(mount_point),
            repair_incomplete=True,
        )
    )

    assert result.mounted
    assert not (mount_point / "partial").exists()
    assert (mount_point / "workspace" / "app.py").read_text(encoding="utf-8") == "complete\n"
    assert (mount_point / IMAGE_MOUNT_MANIFEST_NAME).is_file()


def test_tar_image_archive_mounter_preserves_incomplete_mount_when_repair_fails(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "image.rclip"
    archive_path.write_bytes(b"not-a-tar")
    mount_point = tmp_path / "mounted"
    mount_point.mkdir()
    (mount_point / "partial").write_text("stale", encoding="utf-8")

    result = TarImageArchiveMounter().mount_image_archive(
        WorkerImageMountRequest(
            container_id="ctr-1",
            image_id="image-1",
            archive_path=str(archive_path),
            mount_point=str(mount_point),
            repair_incomplete=True,
        )
    )

    assert result.status is WorkerImageMountStatus.Failed
    assert (mount_point / "partial").read_text(encoding="utf-8") == "stale"


def test_tar_image_archive_mounter_rejects_path_traversal_members(tmp_path: Path) -> None:
    archive_path = tmp_path / "image.rclip"
    with tarfile.open(archive_path, "w") as archive:
        data = b"escape"
        info = tarfile.TarInfo("../escape")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))

    mounted = TarImageArchiveMounter().mount_image_archive(
        WorkerImageMountRequest(
            container_id="ctr-1",
            image_id="image-1",
            archive_path=str(archive_path),
            mount_point=str(tmp_path / "mounted"),
        )
    )

    assert not mounted.mounted
    assert "unsafe image archive member path" in mounted.reason
    assert not (tmp_path / "escape").exists()


def test_brokered_image_source_loader_downloads_presigned_archive(tmp_path: Path) -> None:
    source = tmp_path / "source.rclip"
    source.write_bytes(b"archive")
    source_sha256 = hashlib.sha256(b"archive").hexdigest()
    target = tmp_path / "cache" / "image-1.rclip"
    with _serve_directory(tmp_path) as source_url:
        transport = _FakeWorkerRepositoryTransport(
            posts={
                "/worker-repository/get-cache-origin-credentials": (
                    GetCacheOriginCredentialsResponse(
                        credentials=CacheOriginCredentials(
                            image_archive_url=f"{source_url}/{source.name}",
                            archive_size_bytes=len(b"archive"),
                            archive_sha256=source_sha256,
                        )
                    ).model_dump(mode="json")
                )
            }
        )
        result = BrokeredImageArchiveSourceLoader(
            WorkerRepositoryHttpClient(transport)
        ).load_source_image_archive(
            WorkerImageSourceLoadRequest(
                container_id="ctr-1",
                workspace_id="workspace-1",
                stub_id="stub-1",
                image_id="image-1",
                archive_path=str(target),
                mount_point=str(tmp_path / "mount"),
                cache_path="/images/image-1.rclip",
            )
        )

    assert result.ok
    assert result.bytes_written == len(b"archive")
    assert target.read_bytes() == b"archive"
    assert transport.posts[0][0] == "/worker-repository/get-cache-origin-credentials"
    assert transport.posts[0][1]["workspace_id"] == "workspace-1"
    assert transport.posts[0][1]["container_id"] == "ctr-1"
    assert transport.posts[0][1]["image_id"] == "image-1"


class _FakeWorkerRepositoryTransport:
    def __init__(
        self,
        *,
        posts: Mapping[str, Mapping[str, JsonValue]] | None = None,
    ) -> None:
        self._posts = posts or {}
        self.posts: list[tuple[str, dict[str, JsonValue]]] = []
        self.bearer_token = "bootstrap-token"

    def set_bearer_token(self, token: str) -> None:
        self.bearer_token = token

    def post(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> dict[str, JsonValue]:
        self.posts.append((path, dict(payload)))
        return dict(self._posts.get(path, {"ok": True}))

    def stream(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> Iterator[dict[str, JsonValue]]:
        _ = path, payload
        return iter(())
