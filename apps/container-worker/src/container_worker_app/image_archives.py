from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from cache.server import (
    CacheUnavailableError,
    FileCacheServer,
    WorkerCacheHttpClient,
)
from networking.internal_http import InternalHttpClient
from shared.timestamps import utc_now
from worker.container_startup import (
    WorkerImageArchiveCacheMetadata,
    WorkerImageMountRequest,
    WorkerImageMountResult,
    WorkerImageMountStatus,
    WorkerImageSourceLoadRequest,
    WorkerImageSourceLoadResult,
)
from worker.image_archive_transfer import download_image_archive
from worker.image_runtime import ImageRuntimeClient
from worker.origin_access import CacheOriginCredentialRequest
from worker.repository_client import WorkerRepositoryHttpClient

REGISTRY_CREDENTIAL_REFRESH_INTERVAL_SECONDS = 5 * 60
REGISTRY_CREDENTIAL_REFRESH_LEAD = timedelta(hours=1)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RegistryCredentialLease:
    request: CacheOriginCredentialRequest
    expires_at: datetime


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


@dataclass(slots=True)
class BrokeredClipImageMounter:
    repository: WorkerRepositoryHttpClient
    runtime: ImageRuntimeClient
    _leases: dict[str, _RegistryCredentialLease] = field(default_factory=dict, init=False)
    _lease_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _refresh_started: bool = field(default=False, init=False)

    def mount_image_archive(self, request: WorkerImageMountRequest) -> WorkerImageMountResult:
        credentials = self.repository.get_cache_origin_credentials(
            CacheOriginCredentialRequest(
                workspace_id=request.workspace_id,
                container_id=request.container_id,
                stub_id=request.stub_id,
                image_id=request.image_id,
            )
        ).credentials
        if credentials is None:
            return self._failed(request, "broker did not return image registry credentials")
        if not credentials.ok:
            return self._failed(
                request,
                credentials.error_msg or "broker denied image registry credentials",
            )
        if credentials.archive_sha256 != request.archive_sha256:
            return self._failed(request, "image index identity changed after dispatch")
        if credentials.registry_credentials is None:
            return self._failed(request, "broker did not return image registry credentials")
        try:
            mounted = self.runtime.mount(
                image_id=request.image_id,
                archive_sha256=request.archive_sha256,
                archive_path=Path(request.archive_path),
                mount_point=Path(request.mount_point),
                cache_path=Path(request.cache_path),
                storage_image_ref=credentials.registry_ref,
                credentials=credentials.registry_credentials,
                preload=request.preload,
            )
        except Exception as exc:
            return self._failed(
                request,
                f"lazy image mount failed: {type(exc).__name__}: {exc}",
            )
        expires_at = credentials.registry_credentials.expires_at
        if expires_at is not None:
            self._register_credential_lease(
                request.container_id,
                CacheOriginCredentialRequest(
                    workspace_id=request.workspace_id,
                    container_id=request.container_id,
                    stub_id=request.stub_id,
                    image_id=request.image_id,
                ),
                expires_at,
            )
        return WorkerImageMountResult(
            status=WorkerImageMountStatus.Ready,
            mount_point=str(mounted),
            reason=(
                "image layers prepared and mounted"
                if request.preload
                else "image mounted for lazy layer reads"
            ),
        )

    def _register_credential_lease(
        self,
        container_id: str,
        request: CacheOriginCredentialRequest,
        expires_at: datetime,
    ) -> None:
        with self._lease_lock:
            self._leases[container_id] = _RegistryCredentialLease(request, expires_at)
            if self._refresh_started:
                return
            self._refresh_started = True
        threading.Thread(
            target=self._refresh_registry_credentials,
            daemon=True,
            name="image-registry-credentials",
        ).start()

    def _refresh_registry_credentials(self) -> None:
        while True:
            threading.Event().wait(REGISTRY_CREDENTIAL_REFRESH_INTERVAL_SECONDS)
            refresh_before = utc_now() + REGISTRY_CREDENTIAL_REFRESH_LEAD
            with self._lease_lock:
                due = [
                    (container_id, lease)
                    for container_id, lease in self._leases.items()
                    if lease.expires_at <= refresh_before
                ]
            for container_id, lease in due:
                try:
                    response = self.repository.get_cache_origin_credentials(lease.request)
                    credentials = response.credentials
                    if (
                        credentials is None
                        or not credentials.ok
                        or credentials.registry_credentials is None
                    ):
                        self._drop_credential_lease(container_id, lease)
                        continue
                    self.runtime.update_credentials(credentials.registry_credentials)
                    expires_at = credentials.registry_credentials.expires_at
                    if expires_at is None:
                        self._drop_credential_lease(container_id, lease)
                        continue
                    with self._lease_lock:
                        if self._leases.get(container_id) == lease:
                            self._leases[container_id] = _RegistryCredentialLease(
                                lease.request,
                                expires_at,
                            )
                except Exception:
                    LOGGER.warning(
                        "workload registry credential refresh failed",
                        extra={"container_id": container_id},
                        exc_info=True,
                    )
                    continue

    def _drop_credential_lease(
        self,
        container_id: str,
        lease: _RegistryCredentialLease,
    ) -> None:
        with self._lease_lock:
            if self._leases.get(container_id) == lease:
                self._leases.pop(container_id, None)

    @staticmethod
    def _failed(request: WorkerImageMountRequest, reason: str) -> WorkerImageMountResult:
        return WorkerImageMountResult(
            status=WorkerImageMountStatus.Failed,
            mount_point=request.mount_point,
            reason=reason,
        )
