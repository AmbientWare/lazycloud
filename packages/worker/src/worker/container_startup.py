from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, TypeAdapter
from shared.container_requests import RequestMount
from shared.contracts import ContractModel

from worker.container_execution import (
    ContainerImageLoadResult,
    ContainerMountSetupResult,
)
from worker.events import ContainerRequestContext
from worker.image_archive_cache import (
    WorkerContentCache,
    WorkerImageArchiveLoadResult,
    WorkerImageArchiveLoadStatus,
    WorkerImageArchiveLocalState,
    load_image_archive_from_cache_or_source,
    publish_source_image_archive_to_cache,
)
from worker.image_lifecycle import (
    DEFAULT_IMAGE_ARCHIVE_EXTENSION,
    DEFAULT_IMAGE_CACHE_PATH,
    DEFAULT_IMAGE_MOUNT_ROOT,
    ImageArchiveStorageMode,
    RestoredImageArchiveValidation,
    WorkerImagePaths,
    build_worker_image_paths,
    image_archive_cache_path,
    validate_restored_image_archive,
)
from worker.lifecycle import (
    BindMountSourceDirAction,
    RequestMountLinkPlan,
    RequestMountPlan,
    ensure_bind_mount_source_dirs,
    plan_request_mount_setup,
    plan_request_mounts,
)
from worker.source_code import SourceCodePackageMaterializer

type SocketAddress = tuple[str, int]

_SOCKET_ADDRESS: TypeAdapter[SocketAddress] = TypeAdapter(SocketAddress)


class WorkerImageStartupStatus(StrEnum):
    Skipped = "skipped"
    LocalOrCacheReady = "local-or-cache-ready"
    SourceLoaded = "source-loaded"
    Mounted = "mounted"


class WorkerMountPointStatus(StrEnum):
    Mounted = "mounted"
    Failed = "failed"


class WorkerImageArchiveCacheMetadata(ContractModel):
    content_hash: str = ""
    size_bytes: int = 0
    error: str = ""
    reachable: bool = False


class WorkerImageSourceLoadRequest(ContractModel):
    container_id: str
    image_id: str
    workspace_id: str = ""
    stub_id: str = ""
    archive_path: str
    mount_point: str
    cache_path: str
    reason: str = ""


class WorkerImageSourceLoadResult(ContractModel):
    ok: bool
    archive_path: str
    bytes_written: int = 0
    reason: str = ""


class WorkerImageMountRequest(ContractModel):
    container_id: str
    image_id: str
    archive_path: str
    mount_point: str
    repair_incomplete: bool = False


IMAGE_MOUNT_MANIFEST_NAME = ".image-mount-manifest.json"


class ImageMountManifest(ContractModel):
    schema_version: Literal[1] = 1
    image_id: str = Field(min_length=1)
    archive_size_bytes: int = Field(gt=0)
    archive_entry_count: int = Field(gt=0)


class WorkerImageMountStatus(StrEnum):
    Ready = "ready"
    RepairRequired = "repair-required"
    Failed = "failed"


class WorkerImageMountResult(ContractModel):
    status: WorkerImageMountStatus
    mount_point: str
    reason: str = ""

    @property
    def mounted(self) -> bool:
        return self.status is WorkerImageMountStatus.Ready

    @property
    def repair_required(self) -> bool:
        return self.status is WorkerImageMountStatus.RepairRequired


class WorkerImageStartupRecord(ContractModel):
    status: WorkerImageStartupStatus
    image_id: str = ""
    archive_path: str = ""
    mount_point: str = ""
    cache_load: WorkerImageArchiveLoadResult | None = None
    source_load: WorkerImageSourceLoadResult | None = None
    mount: WorkerImageMountResult | None = None
    reason: str = ""


class WorkerMountPointRequest(ContractModel):
    container_id: str
    mount: RequestMount


class WorkerMountPointResult(ContractModel):
    status: WorkerMountPointStatus
    local_path: str
    mount_path: str
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status is WorkerMountPointStatus.Mounted


class WorkerImageCacheMetadataProvider(Protocol):
    def image_archive_metadata(self, cache_path: str) -> WorkerImageArchiveCacheMetadata: ...


class WorkerImageArchiveSourceLoader(Protocol):
    def load_source_image_archive(
        self,
        request: WorkerImageSourceLoadRequest,
    ) -> WorkerImageSourceLoadResult: ...


class WorkerImageArchiveMounter(Protocol):
    def mount_image_archive(self, request: WorkerImageMountRequest) -> WorkerImageMountResult: ...


class WorkerMountPointBackend(Protocol):
    def mount_request_mount(self, request: WorkerMountPointRequest) -> WorkerMountPointResult: ...

    def unmount_request_mounts(self, container_id: str) -> None: ...

    def unmount_all(self) -> None: ...


@dataclass(slots=True)
class WorkerImageStartupLoader:
    mounter: WorkerImageArchiveMounter
    cache: WorkerContentCache | None = None
    source_loader: WorkerImageArchiveSourceLoader | None = None
    cache_metadata: WorkerImageCacheMetadataProvider | None = None
    image_cache_path: str = DEFAULT_IMAGE_CACHE_PATH
    image_mount_root: str = DEFAULT_IMAGE_MOUNT_ROOT
    image_archive_extension: str = DEFAULT_IMAGE_ARCHIVE_EXTENSION
    storage_mode: ImageArchiveStorageMode = ImageArchiveStorageMode.Local
    publish_source_to_cache: bool = True
    records: list[WorkerImageStartupRecord] = field(default_factory=list)

    def load_image(self, request: ContainerRequestContext) -> ContainerImageLoadResult:
        if not request.image_id:
            self.records.append(
                WorkerImageStartupRecord(
                    status=WorkerImageStartupStatus.Skipped,
                    reason="container request does not include an image id",
                )
            )
            return ContainerImageLoadResult(loaded=True, reason="no image id")

        paths = build_worker_image_paths(
            request.image_id,
            image_cache_path=self.image_cache_path,
            image_mount_root=self.image_mount_root,
            image_archive_extension=self.image_archive_extension,
        )
        cache_path = image_archive_cache_path(
            request.image_id,
            extension=self.image_archive_extension,
        )
        mounted_hit = self._load_mounted_image_hit(request, paths)
        if mounted_hit is not None:
            return mounted_hit

        cache_load = self._load_local_or_cache(request, paths, cache_path)
        source_load: WorkerImageSourceLoadResult | None = None
        if cache_load.should_pull_source:
            source_load = self._load_source(request, paths, cache_path, cache_load.reason)
            if self.cache is not None and self.publish_source_to_cache:
                publish_source_image_archive_to_cache(
                    self.cache,
                    archive_path=paths.local_archive_path,
                    image_id=request.image_id,
                    cache_client_available=True,
                )

        mount = self.mounter.mount_image_archive(
            WorkerImageMountRequest(
                container_id=request.container_id,
                image_id=request.image_id,
                archive_path=paths.local_archive_path,
                mount_point=paths.mount_point,
                repair_incomplete=True,
            )
        )
        if not mount.mounted:
            msg = mount.reason or f"image archive mount failed for {request.image_id}"
            raise RuntimeError(msg)

        reason = mount.reason or (
            source_load.reason if source_load is not None else cache_load.reason
        )
        self.records.append(
            WorkerImageStartupRecord(
                status=WorkerImageStartupStatus.Mounted,
                image_id=request.image_id,
                archive_path=paths.local_archive_path,
                mount_point=paths.mount_point,
                cache_load=cache_load,
                source_load=source_load,
                mount=mount,
                reason=reason,
            )
        )
        return ContainerImageLoadResult(loaded=True, reason=reason)

    def _load_mounted_image_hit(
        self,
        request: ContainerRequestContext,
        paths: WorkerImagePaths,
    ) -> ContainerImageLoadResult | None:
        mount_path = Path(paths.mount_point)
        if not mount_path.exists() and not mount_path.is_symlink():
            return None

        mount = self.mounter.mount_image_archive(
            WorkerImageMountRequest(
                container_id=request.container_id,
                image_id=request.image_id,
                archive_path=paths.local_archive_path,
                mount_point=paths.mount_point,
            )
        )
        if mount.repair_required:
            return None
        if not mount.mounted:
            msg = mount.reason or f"image archive mount failed for {request.image_id}"
            raise RuntimeError(msg)

        reason = mount.reason or "image mount point already exists"
        self.records.append(
            WorkerImageStartupRecord(
                status=WorkerImageStartupStatus.Mounted,
                image_id=request.image_id,
                archive_path=paths.local_archive_path,
                mount_point=paths.mount_point,
                mount=mount,
                reason=reason,
            )
        )
        return ContainerImageLoadResult(loaded=True, reason=reason)

    def _load_local_or_cache(
        self,
        request: ContainerRequestContext,
        paths: WorkerImagePaths,
        cache_path: str,
    ) -> WorkerImageArchiveLoadResult:
        if self.cache is None:
            return WorkerImageArchiveLoadResult(
                status=WorkerImageArchiveLoadStatus.SourceFallback,
                archive_path=paths.local_archive_path,
                image_id=request.image_id,
                reason="image content cache is not configured",
            )
        metadata = self._cache_metadata(cache_path)
        return load_image_archive_from_cache_or_source(
            self.cache,
            archive_path=paths.local_archive_path,
            image_id=request.image_id,
            cache_path=cache_path,
            validator=lambda path, _plan: self._validate_archive(request.image_id, path),
            local_state=self._local_state(paths.local_archive_path),
            metadata_hash=metadata.content_hash,
            metadata_size_bytes=metadata.size_bytes,
            metadata_error=metadata.error or None,
            cached_reachable=metadata.reachable,
        )

    def _load_source(
        self,
        request: ContainerRequestContext,
        paths: WorkerImagePaths,
        cache_path: str,
        reason: str,
    ) -> WorkerImageSourceLoadResult:
        if self.source_loader is None:
            msg = f"image source loader is required after cache miss: {reason}"
            raise RuntimeError(msg)
        Path(paths.local_archive_path).parent.mkdir(parents=True, exist_ok=True)
        loaded = self.source_loader.load_source_image_archive(
            WorkerImageSourceLoadRequest(
                container_id=request.container_id,
                image_id=request.image_id,
                workspace_id=request.workspace_id,
                stub_id=request.stub_id,
                archive_path=paths.local_archive_path,
                mount_point=paths.mount_point,
                cache_path=cache_path,
                reason=reason,
            )
        )
        if not loaded.ok:
            msg = loaded.reason or f"image source load failed for {request.image_id}"
            raise RuntimeError(msg)
        return loaded

    def _cache_metadata(self, cache_path: str) -> WorkerImageArchiveCacheMetadata:
        if self.cache_metadata is None:
            return WorkerImageArchiveCacheMetadata(error="cache metadata provider is unavailable")
        return self.cache_metadata.image_archive_metadata(cache_path)

    def _local_state(self, archive_path: str) -> WorkerImageArchiveLocalState:
        path = Path(archive_path)
        exists = path.exists()
        return WorkerImageArchiveLocalState(
            exists=exists,
            is_dir=path.is_dir() if exists else False,
            size_bytes=path.stat().st_size if exists and path.is_file() else 0,
            storage_mode=self.storage_mode,
        )

    def _validate_archive(self, image_id: str, path: Path) -> RestoredImageArchiveValidation:
        return validate_restored_image_archive(
            image_id=image_id,
            size_bytes=path.stat().st_size if path.exists() else 0,
            metadata_valid=True,
            storage_mode=self.storage_mode,
            has_image_metadata=self.storage_mode is not ImageArchiveStorageMode.Oci,
            layer_count=1 if self.storage_mode is ImageArchiveStorageMode.Oci else 0,
            decompressed_hash_count=1 if self.storage_mode is ImageArchiveStorageMode.Oci else 0,
        )


@dataclass(slots=True)
class WorkerRequestMountPreparer:
    mountpoint_backend: WorkerMountPointBackend | None = None
    source_materializer: SourceCodePackageMaterializer | None = None
    workspace_storage_available: bool = False

    def setup_mounts(self, request: ContainerRequestContext) -> ContainerMountSetupResult:
        if not request.mounts:
            return ContainerMountSetupResult(reason="container request has no mounts")
        request_mounts = self._materialize_source_mounts(request)
        setup = plan_request_mount_setup(
            request_mounts,
            container_id=request.container_id,
            workspace_name=request.workspace_name or request.workspace_id,
            workspace_storage_available=(
                self.workspace_storage_available or request.workspace_storage_available
            ),
            workspace_storage_base_mount_path=request.workspace_storage_base_mount_path,
        )
        try:
            for mount in setup.mountpoint_mounts:
                self._mount_mountpoint(request.container_id, mount)

            plan = plan_request_mounts(setup.mounts)
            self._create_source_dirs(plan)
            for link in plan.symlinks:
                self._create_symlink(link)

            return ContainerMountSetupResult(
                mounts=list(plan.mounts),
                oci_mounts=list(plan.oci_mounts),
                volume_cache_map=dict(plan.volume_cache_map),
                source_dirs_to_create=list(plan.source_dirs_to_create),
                symlinks=list(plan.symlinks),
                reason="container request mounts prepared",
            )
        except Exception as setup_error:
            if self.mountpoint_backend is None:
                raise
            try:
                self.mountpoint_backend.unmount_request_mounts(request.container_id)
            except Exception as cleanup_error:
                raise ExceptionGroup(
                    "request mount setup and rollback failed",
                    [setup_error, cleanup_error],
                ) from setup_error
            raise

    def _mount_mountpoint(self, container_id: str, mount: RequestMount) -> None:
        if self.mountpoint_backend is None:
            msg = f"mountpoint backend is required for {mount.mount_path}"
            raise RuntimeError(msg)
        result = self.mountpoint_backend.mount_request_mount(
            WorkerMountPointRequest(container_id=container_id, mount=mount)
        )
        if not result.ok:
            msg = result.reason or f"mountpoint setup failed for {mount.mount_path}"
            raise RuntimeError(msg)

    def _materialize_source_mounts(self, request: ContainerRequestContext) -> list[RequestMount]:
        if self.source_materializer is None:
            self.source_materializer = SourceCodePackageMaterializer()
        mounts: list[RequestMount] = []
        for mount in request.mounts:
            if mount.source_object_id:
                materialized = self.source_materializer.materialize(request, mount)
                mounts.append(mount.model_copy(update={"local_path": materialized.workspace_path}))
            else:
                mounts.append(mount)
        return mounts

    def _create_source_dirs(self, plan: RequestMountPlan) -> None:
        results = ensure_bind_mount_source_dirs([item.mount for item in plan.mounts])
        failures = [
            item.local_path
            for item in results
            if item.action is BindMountSourceDirAction.Create and not Path(item.local_path).exists()
        ]
        if failures:
            msg = f"failed to create bind mount source dirs: {', '.join(failures)}"
            raise RuntimeError(msg)

    def _create_symlink(self, link: RequestMountLinkPlan) -> None:
        target = Path(link.target)
        path = Path(link.link_path)
        if link.replace_existing and (path.exists() or path.is_symlink()):
            if path.is_dir() and not path.is_symlink():
                msg = f"cannot replace directory symlink target: {path}"
                raise RuntimeError(msg)
            path.unlink()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)


@dataclass(slots=True)
class HostPortAllocator:
    host: str = ""

    def allocate_ports(self, count: int) -> list[int]:
        if count < 0:
            msg = "port allocation count cannot be negative"
            raise ValueError(msg)
        sockets: list[socket.socket] = []
        try:
            for _ in range(count):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind((self.host, 0))
                sockets.append(sock)
            return [
                _SOCKET_ADDRESS.validate_json(json.dumps(sock.getsockname()))[1] for sock in sockets
            ]
        finally:
            for sock in sockets:
                sock.close()
