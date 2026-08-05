from __future__ import annotations

import posixpath
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from cache.server import (
    CacheUnavailableError,
    FileCacheServer,
    WorkerCacheHttpClient,
)
from networking.internal_http import InternalHttpClient
from worker.container_startup import (
    IMAGE_MOUNT_MANIFEST_NAME,
    ImageMountManifest,
    WorkerImageArchiveCacheMetadata,
    WorkerImageMountRequest,
    WorkerImageMountResult,
    WorkerImageMountStatus,
    WorkerImageSourceLoadRequest,
    WorkerImageSourceLoadResult,
    read_image_mount_manifest,
)
from worker.image_archive_transfer import download_image_archive
from worker.origin_access import CacheOriginCredentialRequest
from worker.repository_client import WorkerRepositoryHttpClient


@dataclass(slots=True)
class BrokeredImageArchiveSourceLoader:
    repository: WorkerRepositoryHttpClient
    internal_http: InternalHttpClient = field(default_factory=InternalHttpClient)
    timeout_seconds: float = 60.0

    def load_source_image_archive(
        self,
        request: WorkerImageSourceLoadRequest,
    ) -> WorkerImageSourceLoadResult:
        credentials = self.repository.get_cache_origin_credentials(
            CacheOriginCredentialRequest(
                workspace_id=request.workspace_id,
                container_id=request.container_id,
                stub_id=request.stub_id,
                image_id=request.image_id,
            )
        ).credentials
        if credentials is None:
            return WorkerImageSourceLoadResult(
                ok=False,
                archive_path=request.archive_path,
                reason="broker did not return image archive credentials",
            )
        if not credentials.ok:
            return WorkerImageSourceLoadResult(
                ok=False,
                archive_path=request.archive_path,
                reason=credentials.error_msg or "broker denied image archive credentials",
            )
        if credentials.image_archive_url:
            return self._download_url(
                credentials.image_archive_url,
                request.archive_path,
                archive_size_bytes=credentials.archive_size_bytes,
                archive_sha256=credentials.archive_sha256,
            )
        return WorkerImageSourceLoadResult(
            ok=False,
            archive_path=request.archive_path,
            reason="broker did not return an image archive URL",
        )

    def _download_url(
        self,
        url: str,
        archive_path: str,
        *,
        archive_size_bytes: int,
        archive_sha256: str,
    ) -> WorkerImageSourceLoadResult:
        target = Path(archive_path)
        try:
            bytes_written = download_image_archive(
                self.internal_http,
                url,
                target,
                archive_size_bytes=archive_size_bytes,
                archive_sha256=archive_sha256,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as exc:
            return WorkerImageSourceLoadResult(
                ok=False,
                archive_path=archive_path,
                reason=f"brokered image archive download failed: {type(exc).__name__}: {exc}",
            )
        return WorkerImageSourceLoadResult(
            ok=True,
            archive_path=str(target),
            archive_sha256=archive_sha256,
            bytes_written=bytes_written,
            reason="brokered image archive downloaded and verified",
        )


@dataclass(slots=True)
class CacheServerImageArchiveMetadataProvider:
    cache: FileCacheServer | WorkerCacheHttpClient

    def image_archive_metadata(self, cache_path: str) -> WorkerImageArchiveCacheMetadata:
        try:
            metadata = self.cache.content_metadata(cache_path)
        except CacheUnavailableError as exc:
            return WorkerImageArchiveCacheMetadata(error=str(exc), reachable=False)
        if metadata is None or not metadata.complete:
            return WorkerImageArchiveCacheMetadata(error="content_not_found", reachable=False)
        return WorkerImageArchiveCacheMetadata(
            content_hash=metadata.content_hash,
            size_bytes=metadata.size_bytes,
            reachable=True,
        )


def _normalize_image_archive_member_name(name: str) -> str | None:
    if not name or "\x00" in name:
        msg = f"unsafe image archive member name: {name!r}"
        raise ValueError(msg)
    normalized = posixpath.normpath(name)
    if normalized == ".":
        return None
    if posixpath.isabs(name) or normalized == ".." or normalized.startswith("../"):
        msg = f"unsafe image archive member path: {name!r}"
        raise ValueError(msg)
    return normalized


def _normalize_image_archive_hardlink_target(linkname: str) -> str:
    if not linkname or "\x00" in linkname:
        msg = f"unsafe image archive hard link target: {linkname!r}"
        raise ValueError(msg)
    target = linkname[1:] if posixpath.isabs(linkname) else linkname
    normalized = posixpath.normpath(target)
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        msg = f"unsafe image archive hard link target: {linkname!r}"
        raise ValueError(msg)
    return normalized


def _image_root_relative_symlink_target(member_name: str, linkname: str) -> str:
    if not linkname or "\x00" in linkname:
        msg = f"unsafe image archive symbolic link target: {linkname!r}"
        raise ValueError(msg)
    member_parent = posixpath.dirname(member_name)
    member_parent_root = f"/{member_parent}" if member_parent else "/"
    if posixpath.isabs(linkname):
        target_root_path = posixpath.normpath(linkname)
    else:
        target_root_path = posixpath.normpath(posixpath.join(member_parent_root, linkname))
    if not target_root_path.startswith("/"):
        target_root_path = f"/{target_root_path}"
    return posixpath.relpath(target_root_path, member_parent_root)


def _image_archive_tar_filter(
    member: tarfile.TarInfo,
    destination: str,
) -> tarfile.TarInfo | None:
    del destination
    normalized_name = _normalize_image_archive_member_name(member.name)
    if normalized_name is None:
        return None

    updated_name = member.name
    updated_linkname = member.linkname
    if normalized_name != member.name:
        updated_name = normalized_name
    if member.issym():
        updated_linkname = _image_root_relative_symlink_target(
            normalized_name,
            member.linkname,
        )
    elif member.islnk():
        updated_linkname = _normalize_image_archive_hardlink_target(member.linkname)

    if updated_name == member.name and updated_linkname == member.linkname:
        return member
    return member.replace(name=updated_name, linkname=updated_linkname)


@dataclass(slots=True)
class TarImageArchiveMounter:
    clear_existing: bool = False

    def mount_image_archive(self, request: WorkerImageMountRequest) -> WorkerImageMountResult:
        archive_path = Path(request.archive_path)
        mount_point = Path(request.mount_point)
        mount_exists = mount_point.exists() or mount_point.is_symlink()
        if mount_exists and not self.clear_existing:
            manifest = _read_image_mount_manifest(
                mount_point,
                image_id=request.image_id,
                archive_sha256=request.archive_sha256,
                archive_path=archive_path,
            )
            if manifest is not None:
                mount_point.touch(exist_ok=True)
                if archive_path.is_file():
                    archive_path.touch(exist_ok=True)
                return WorkerImageMountResult(
                    status=WorkerImageMountStatus.Ready,
                    mount_point=str(mount_point),
                    reason="complete image mount already exists",
                )
            if not request.repair_incomplete:
                return WorkerImageMountResult(
                    status=WorkerImageMountStatus.RepairRequired,
                    mount_point=str(mount_point),
                    reason="image mount is incomplete and requires archive materialization",
                )
        if not archive_path.exists() or not archive_path.is_file():
            return WorkerImageMountResult(
                status=WorkerImageMountStatus.Failed,
                mount_point=str(mount_point),
                reason=f"image archive not found: {archive_path}",
            )
        if not tarfile.is_tarfile(archive_path):
            return WorkerImageMountResult(
                status=WorkerImageMountStatus.Failed,
                mount_point=str(mount_point),
                reason=f"image archive is not a tar archive: {archive_path}",
            )
        temp_mount = mount_point.with_name(f".{mount_point.name}.{uuid4().hex}.tmp")
        temp_mount.mkdir(parents=True, exist_ok=False)
        try:
            with tarfile.open(archive_path) as archive:
                members = archive.getmembers()
                if not members:
                    raise RuntimeError("image archive does not contain filesystem entries")
                archive.extractall(temp_mount, filter=_image_archive_tar_filter)
            manifest_path = temp_mount / IMAGE_MOUNT_MANIFEST_NAME
            _remove_image_mount_path(manifest_path)
            manifest_path.write_text(
                ImageMountManifest(
                    image_id=request.image_id,
                    archive_sha256=request.archive_sha256,
                    archive_size_bytes=archive_path.stat().st_size,
                    archive_entry_count=len(members),
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )
            if mount_point.exists() or mount_point.is_symlink():
                if not self.clear_existing:
                    existing = _read_image_mount_manifest(
                        mount_point,
                        image_id=request.image_id,
                        archive_sha256=request.archive_sha256,
                        archive_path=archive_path,
                    )
                    if existing is not None:
                        mount_point.touch(exist_ok=True)
                        if archive_path.is_file():
                            archive_path.touch(exist_ok=True)
                        shutil.rmtree(temp_mount, ignore_errors=True)
                        return WorkerImageMountResult(
                            status=WorkerImageMountStatus.Ready,
                            mount_point=str(mount_point),
                            reason="complete image mount already exists",
                        )
                _remove_image_mount_path(mount_point)
            mount_point.parent.mkdir(parents=True, exist_ok=True)
            try:
                temp_mount.replace(mount_point)
            except OSError:
                concurrent = _read_image_mount_manifest(
                    mount_point,
                    image_id=request.image_id,
                    archive_sha256=request.archive_sha256,
                    archive_path=archive_path,
                )
                if concurrent is None:
                    raise
                mount_point.touch(exist_ok=True)
                archive_path.touch(exist_ok=True)
                shutil.rmtree(temp_mount, ignore_errors=True)
                return WorkerImageMountResult(
                    status=WorkerImageMountStatus.Ready,
                    mount_point=str(mount_point),
                    reason="complete image mount won concurrent materialization",
                )
        except Exception as exc:
            shutil.rmtree(temp_mount, ignore_errors=True)
            return WorkerImageMountResult(
                status=WorkerImageMountStatus.Failed,
                mount_point=str(mount_point),
                reason=f"image archive materialization failed: {type(exc).__name__}: {exc}",
            )
        return WorkerImageMountResult(
            status=WorkerImageMountStatus.Ready,
            mount_point=str(mount_point),
            reason="image archive materialized",
        )


def _read_image_mount_manifest(
    mount_point: Path,
    *,
    image_id: str,
    archive_sha256: str,
    archive_path: Path,
) -> ImageMountManifest | None:
    manifest = read_image_mount_manifest(mount_point)
    if manifest is None:
        return None
    if manifest.image_id != image_id:
        return None
    # Two archives for one image id can share a size, so the size check alone can
    # leave a repaired mount serving the bytes it was supposed to replace.
    if archive_sha256 and manifest.archive_sha256 and manifest.archive_sha256 != archive_sha256:
        return None
    if archive_path.is_file() and archive_path.stat().st_size != manifest.archive_size_bytes:
        return None
    return manifest


def _remove_image_mount_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink(missing_ok=True)
