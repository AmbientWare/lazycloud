from __future__ import annotations

import hashlib
import http.client
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from pydantic import Field
from shared.contracts import ContractModel
from shared.managed_runtime_integrity import managed_package_source_digest
from shared.scheduling import SchedulerWorkerRequest
from shared.timestamps import utc_now

from worker.container_checkpoints import ContainerImageArchiver
from worker.container_execution import WorkerAddressPublisher
from worker.container_service.models import WorkerContainerServiceInstance
from worker.container_service.protocols import WorkerContainerInstanceStore
from worker.events import ContainerRequestContext
from worker.image_archive_transfer import (
    image_archive_file_identity,
    upload_image_archive,
)
from worker.image_build_architecture import (
    ImageBuildArchitectureError,
    ImageBuildArchitecturePreparer,
    ImageBuildArchitectureRuntime,
)
from worker.image_build_requests import (
    IMAGE_BUILD_REQUEST_KIND,
    ImageBuildContainerBuildOptions,
    ImageBuildContainerCredentialMetadata,
    ImageBuildSchedulerCredentialSource,
)
from worker.image_build_runtime_credentials import ImageBuildCredentialLoader
from worker.image_build_scratch import (
    IMAGE_BUILD_SCRATCH_MONITOR_SECONDS,
    ImageBuildScratchLease,
    ImageBuildScratchManager,
)
from worker.image_lifecycle import (
    BuildahDirectoryPlan,
    BuildahStorageDriver,
    plan_buildah_directories,
    plan_buildah_environment,
    plan_buildah_storage_config,
)
from worker.origin_access import (
    ImageArchiveUploadCredentialRequest,
    ImageArchiveUploadCredentials,
)
from worker.repository_payloads import (
    ImageBuildPrivateInputs,
    ImageBuildRegistryAuth,
    PrepareImageBuildContextDownloadRequest,
    PrepareImageBuildContextDownloadResponse,
)

ImageBuildLog = Callable[[str], None]
IMAGE_ARCHIVE_UPLOAD_TIMEOUT_SECONDS = 120
MAX_IMAGE_BUILD_CONTEXT_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_IMAGE_BUILD_CONTEXT_MEMBERS = 10_000
MAX_IMAGE_BUILD_CONTEXT_MEMBER_BYTES = 128 * 1024 * 1024
MAX_IMAGE_BUILD_CONTEXT_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_IMAGE_BUILD_CONTEXT_PATH_LENGTH = 1024
MAX_IMAGE_BUILD_CONTEXT_PATH_DEPTH = 64


class WorkerImageBuildStatus(StrEnum):
    Running = "running"
    Complete = "complete"
    Failed = "failed"


class WorkerImageBuildSessionPayload(ContractModel):
    clip_version: int = 2
    ttl_seconds: int = 0
    keepalive_interval_seconds: int = 0


class WorkerImageBuildRequestPayload(ContractModel):
    kind: str = IMAGE_BUILD_REQUEST_KIND
    workspace_id: str = ""
    build_id: str
    image_id: str
    tag: str = ""
    dockerfile_path: str = ""
    manifest_path: str = ""
    build_dir: str = ""
    build_options: ImageBuildContainerBuildOptions
    credential_metadata: ImageBuildContainerCredentialMetadata = Field(
        default_factory=ImageBuildContainerCredentialMetadata
    )
    archive_upload_capability: str = ""
    session: WorkerImageBuildSessionPayload = Field(default_factory=WorkerImageBuildSessionPayload)
    env: list[str] = Field(default_factory=list)


class WorkerImageArchiveBuildResult(ContractModel):
    ok: bool
    image_id: str
    archive_path: str = ""
    logs: list[str] = Field(default_factory=list)
    error_message: str = ""


class WorkerImageBuildContextLoadResult(ContractModel):
    ok: bool
    object_id: str
    files: list[str] = Field(default_factory=list)
    error_message: str = ""


class WorkerImageArchivePublishResult(ContractModel):
    ok: bool
    image_id: str
    object_key: str = ""
    bucket: str = ""
    size_bytes: int = 0
    sha256: str = ""
    error_message: str = ""


class WorkerImageBuildExecutionResult(ContractModel):
    ok: bool
    container_id: str
    image_id: str
    build_id: str
    archive_path: str = ""
    object_key: str = ""
    archive_size_bytes: int = 0
    archive_sha256: str = ""
    status: WorkerImageBuildStatus = WorkerImageBuildStatus.Failed
    logs: list[str] = Field(default_factory=list)
    error_message: str = ""


class WorkerImageBuilder(Protocol):
    def build_image_archive(
        self,
        payload: WorkerImageBuildRequestPayload,
        *,
        container_id: str,
        registry_auth: ImageBuildRegistryAuth | None,
        build_args: dict[str, str],
        log: ImageBuildLog,
    ) -> WorkerImageArchiveBuildResult: ...


class WorkerImageBuildContextLoader(Protocol):
    def extract_build_context(
        self,
        object_id: str,
        target_dir: Path,
        *,
        workspace_id: str,
        build_id: str,
        container_id: str,
    ) -> WorkerImageBuildContextLoadResult: ...


class ImageBuildContextDownloadClient(Protocol):
    def prepare_image_build_context_download(
        self,
        request: PrepareImageBuildContextDownloadRequest,
    ) -> PrepareImageBuildContextDownloadResponse: ...


class ImageBuildContextDownloadError(RuntimeError):
    pass


class WorkerImageArchivePublisher(Protocol):
    def publish_image_archive(
        self,
        *,
        image_id: str,
        build_id: str = "",
        container_id: str = "",
        upload_capability: str = "",
        archive_path: Path,
        workspace_id: str = "",
        stub_id: str = "",
    ) -> WorkerImageArchivePublishResult: ...


class ImageArchiveUploadCredentialsResponseLike(Protocol):
    credentials: ImageArchiveUploadCredentials | None


class ImageArchiveUploadCredentialClient(Protocol):
    def get_image_archive_upload_credentials(
        self,
        request: ImageArchiveUploadCredentialRequest,
    ) -> ImageArchiveUploadCredentialsResponseLike: ...


def is_image_build_scheduler_request(request: SchedulerWorkerRequest) -> bool:
    return str(request.payload.get("kind") or "") == IMAGE_BUILD_REQUEST_KIND


@dataclass(slots=True)
class WorkerImageBuildExecutionService:
    address_publisher: WorkerAddressPublisher
    instances: WorkerContainerInstanceStore
    builder: WorkerImageBuilder
    publisher: WorkerImageArchivePublisher
    credential_loader: ImageBuildCredentialLoader | None = None

    def execute(self, request: SchedulerWorkerRequest) -> WorkerImageBuildExecutionResult:
        payload = WorkerImageBuildRequestPayload.model_validate(request.payload).model_copy(
            update={"workspace_id": request.workspace_id}
        )
        instance = self._build_instance(request, payload)
        logs: list[str] = []
        sensitive_values: tuple[str, ...] = ()

        def log(message: str) -> None:
            clean = _redact_image_build_text(
                message,
                sensitive_values=sensitive_values,
            ).rstrip()
            if not clean:
                return
            if logs and logs[-1] == clean:
                return
            logs.append(clean)
            stored = self.instances.get_container_instance(request.container_id) or instance
            if stored.log_messages and stored.log_messages[-1] == clean:
                return
            stored.log_messages = [*stored.log_messages, clean]
            self.instances.save_container_instance(stored)

        try:
            self.address_publisher.publish_worker_address(_request_context(request, payload))
            self.instances.save_container_instance(instance)
            log("image build worker request accepted")
            _require_matching_image_architecture(request, payload)
            _require_matching_managed_packages(payload.build_options.managed_package_digest)
            private_inputs = self._private_inputs(request, payload)
            sensitive_values = _image_build_private_values(private_inputs)
            build = self.builder.build_image_archive(
                payload,
                container_id=request.container_id,
                registry_auth=private_inputs.registry_auth,
                build_args=private_inputs.build_args,
                log=log,
            )
            if not build.ok:
                return self._finish(
                    instance,
                    payload,
                    status=WorkerImageBuildStatus.Failed,
                    exit_code=1,
                    archive_path=build.archive_path,
                    logs=[
                        *logs,
                        *(
                            _redact_image_build_text(
                                item,
                                sensitive_values=sensitive_values,
                            )
                            for item in build.logs
                        ),
                    ],
                    error_message=_redact_image_build_text(
                        build.error_message or "image archive build failed",
                        sensitive_values=sensitive_values,
                    ),
                )
            published = self.publisher.publish_image_archive(
                image_id=payload.image_id,
                build_id=payload.build_id,
                container_id=request.container_id,
                upload_capability=payload.archive_upload_capability,
                archive_path=Path(build.archive_path),
                workspace_id=request.workspace_id,
                stub_id=request.stub_id,
            )
            if not published.ok:
                return self._finish(
                    instance,
                    payload,
                    status=WorkerImageBuildStatus.Failed,
                    exit_code=1,
                    archive_path=build.archive_path,
                    logs=[
                        *logs,
                        *(
                            _redact_image_build_text(
                                item,
                                sensitive_values=sensitive_values,
                            )
                            for item in build.logs
                        ),
                    ],
                    error_message=_redact_image_build_text(
                        published.error_message or "image archive publication failed",
                        sensitive_values=sensitive_values,
                    ),
                )
            log(
                "image archive published: "
                f"{published.bucket}/{published.object_key} ({published.size_bytes} bytes)"
            )
            return self._finish(
                instance,
                payload,
                status=WorkerImageBuildStatus.Complete,
                exit_code=0,
                archive_path=build.archive_path,
                object_key=published.object_key,
                archive_size_bytes=published.size_bytes,
                archive_sha256=published.sha256,
                logs=[
                    *logs,
                    *(
                        _redact_image_build_text(
                            item,
                            sensitive_values=sensitive_values,
                        )
                        for item in build.logs
                    ),
                ],
            )
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            return self._finish(
                instance,
                payload,
                status=WorkerImageBuildStatus.Failed,
                exit_code=1,
                logs=logs,
                error_message=_redact_image_build_text(
                    f"{type(exc).__name__}: {exc}",
                    sensitive_values=sensitive_values,
                ),
            )

    def _private_inputs(
        self,
        request: SchedulerWorkerRequest,
        payload: WorkerImageBuildRequestPayload,
    ) -> ImageBuildPrivateInputs:
        metadata = payload.credential_metadata
        if metadata.source is not ImageBuildSchedulerCredentialSource.EphemeralPrivateInputs:
            return ImageBuildPrivateInputs()
        if self.credential_loader is None:
            raise RuntimeError("image build private input loader is not configured")
        private_inputs = self.credential_loader.load(
            workspace_id=request.workspace_id,
            build_id=payload.build_id,
            container_id=request.container_id,
            registry=metadata.registry,
            cache_key=metadata.cache_key,
        )
        if private_inputs.empty:
            raise RuntimeError("image build private inputs expired or were already consumed")
        return private_inputs

    def _build_instance(
        self,
        request: SchedulerWorkerRequest,
        payload: WorkerImageBuildRequestPayload,
    ) -> WorkerContainerServiceInstance:
        return WorkerContainerServiceInstance(
            container_id=request.container_id,
            root_path="",
            cwd="/workspace",
            env=list(payload.env),
            request_env=list(payload.env),
            build_request=True,
            status=WorkerImageBuildStatus.Running.value,
            exit_code=0,
            workspace_id=request.workspace_id,
            stub_id=request.stub_id,
            image_id=payload.image_id,
        )

    def _finish(
        self,
        instance: WorkerContainerServiceInstance,
        payload: WorkerImageBuildRequestPayload,
        *,
        status: WorkerImageBuildStatus,
        exit_code: int,
        archive_path: str = "",
        object_key: str = "",
        archive_size_bytes: int = 0,
        archive_sha256: str = "",
        logs: list[str] | None = None,
        error_message: str = "",
    ) -> WorkerImageBuildExecutionResult:
        stored = self.instances.get_container_instance(instance.container_id) or instance
        stored.status = status.value
        stored.exit_code = exit_code
        stored.build_archive_object_key = object_key
        stored.build_archive_size_bytes = archive_size_bytes
        stored.build_archive_sha256 = archive_sha256
        stored.build_error_message = error_message
        if error_message:
            stored.log_messages = _append_unique_log(stored.log_messages, error_message)
        self.instances.save_container_instance(stored)
        result_logs = list(logs or [])
        if error_message:
            result_logs = _append_unique_log(result_logs, error_message)
        return WorkerImageBuildExecutionResult(
            ok=status is WorkerImageBuildStatus.Complete,
            container_id=instance.container_id,
            image_id=payload.image_id,
            build_id=payload.build_id,
            archive_path=archive_path,
            object_key=object_key,
            archive_size_bytes=archive_size_bytes,
            archive_sha256=archive_sha256,
            status=status,
            logs=result_logs,
            error_message=error_message,
        )


def _require_matching_managed_packages(expected_digest: str) -> None:
    worker_digest = managed_package_source_digest()
    if worker_digest == expected_digest:
        return
    resolved_digest = worker_digest or "unavailable"
    msg = (
        "managed package digest mismatch: "
        f"control plane expected {expected_digest}, image worker resolved {resolved_digest}"
    )
    raise RuntimeError(msg)


def _require_matching_image_architecture(
    request: SchedulerWorkerRequest,
    payload: WorkerImageBuildRequestPayload,
) -> None:
    scheduler_architecture = request.architecture
    build_architecture = payload.build_options.architecture.value
    if scheduler_architecture == build_architecture:
        return
    raise RuntimeError(
        "image build architecture mismatch: "
        f"scheduler requested {scheduler_architecture}, "
        f"build payload requested {build_architecture}"
    )


@dataclass(slots=True)
class RepositoryImageBuildContextLoader:
    repository: ImageBuildContextDownloadClient
    timeout_seconds: float = 60.0

    def extract_build_context(
        self,
        object_id: str,
        target_dir: Path,
        *,
        workspace_id: str,
        build_id: str,
        container_id: str,
    ) -> WorkerImageBuildContextLoadResult:
        try:
            plan = self.repository.prepare_image_build_context_download(
                PrepareImageBuildContextDownloadRequest(
                    workspace_id=workspace_id,
                    build_id=build_id,
                    container_id=container_id,
                    object_id=object_id,
                )
            )
            if plan.object_id != object_id:
                raise ValueError("build context download plan object does not match request")
            if not plan.download_url:
                raise ValueError("build context download plan URL is missing")
            if plan.expires_at is None or plan.expires_at <= utc_now():
                raise ValueError("build context download plan is expired")
            if plan.content_length > MAX_IMAGE_BUILD_CONTEXT_ARCHIVE_BYTES:
                raise ValueError(
                    "build context archive exceeds maximum size of "
                    f"{MAX_IMAGE_BUILD_CONTEXT_ARCHIVE_BYTES} bytes"
                )
            with tempfile.TemporaryDirectory(prefix="lazycloud-worker-context-") as directory:
                archive_path = Path(directory) / "context.zip"
                _download_image_build_context(
                    plan,
                    archive_path,
                    timeout_seconds=self.timeout_seconds,
                )
                files = _extract_zip_context(archive_path, target_dir)
        except Exception as exc:
            return WorkerImageBuildContextLoadResult(
                ok=False,
                object_id=object_id,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        return WorkerImageBuildContextLoadResult(
            ok=True,
            object_id=object_id,
            files=files,
        )


def _download_image_build_context(
    plan: PrepareImageBuildContextDownloadResponse,
    target: Path,
    *,
    timeout_seconds: float,
) -> None:
    parsed = urlparse(plan.download_url)
    if parsed.scheme not in {"http", "https"}:
        raise ImageBuildContextDownloadError("build context download URL must use HTTP or HTTPS")
    if not plan.sha256:
        raise ImageBuildContextDownloadError("build context download SHA-256 is missing")
    if parsed.hostname is None:
        raise ImageBuildContextDownloadError("build context download URL hostname is required")
    connection: http.client.HTTPConnection
    if parsed.scheme == "https":
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port,
            timeout=timeout_seconds,
        )
    else:
        connection = http.client.HTTPConnection(
            parsed.hostname,
            parsed.port,
            timeout=timeout_seconds,
        )
    request_target = parsed.path or "/"
    if parsed.query:
        request_target = f"{request_target}?{parsed.query}"
    try:
        connection.request("GET", request_target)
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            raise ImageBuildContextDownloadError(
                f"build context download returned HTTP {response.status}"
            )
        with target.open("wb") as output:
            header = response.getheader("Content-Length")
            if header is not None:
                try:
                    response_length = int(header)
                except ValueError as exc:
                    raise ImageBuildContextDownloadError(
                        "build context response Content-Length is invalid"
                    ) from exc
                if response_length != plan.content_length:
                    raise ImageBuildContextDownloadError(
                        "build context response Content-Length does not match object metadata"
                    )
            digest = hashlib.sha256()
            bytes_written = 0
            while chunk := response.read(1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > plan.content_length:
                    raise ImageBuildContextDownloadError(
                        "build context response exceeds object metadata size"
                    )
                if bytes_written > MAX_IMAGE_BUILD_CONTEXT_ARCHIVE_BYTES:
                    raise ImageBuildContextDownloadError(
                        "build context response exceeds maximum archive size"
                    )
                output.write(chunk)
                digest.update(chunk)
    except ImageBuildContextDownloadError:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise ImageBuildContextDownloadError(
            f"build context download failed: {type(exc).__name__}"
        ) from None
    finally:
        connection.close()
    if bytes_written != plan.content_length:
        target.unlink(missing_ok=True)
        raise ImageBuildContextDownloadError("build context response body is incomplete")
    if digest.hexdigest() != plan.sha256:
        target.unlink(missing_ok=True)
        raise ImageBuildContextDownloadError("build context response SHA-256 does not match")


@dataclass(slots=True)
class BuildahWorkerImageBuilder:
    archiver: ContainerImageArchiver
    scratch: ImageBuildScratchManager
    context_loader: WorkerImageBuildContextLoader | None = None
    architecture_preparer: ImageBuildArchitecturePreparer = field(
        default_factory=ImageBuildArchitectureRuntime
    )
    buildah_binary: str = "buildah"
    storage_driver: BuildahStorageDriver = BuildahStorageDriver.Overlay
    fallback_storage_driver: BuildahStorageDriver = BuildahStorageDriver.Vfs

    def build_image_archive(
        self,
        payload: WorkerImageBuildRequestPayload,
        *,
        container_id: str,
        registry_auth: ImageBuildRegistryAuth | None = None,
        build_args: dict[str, str] | None = None,
        log: ImageBuildLog,
    ) -> WorkerImageArchiveBuildResult:
        if not payload.image_id:
            return WorkerImageArchiveBuildResult(
                ok=False,
                image_id=payload.image_id,
                error_message="image id is required",
            )
        if shutil.which(self.buildah_binary) is None:
            return WorkerImageArchiveBuildResult(
                ok=False,
                image_id=payload.image_id,
                error_message=f"buildah binary not found: {self.buildah_binary}",
            )
        try:
            self.architecture_preparer.ensure(payload.build_options.architecture)
        except ImageBuildArchitectureError as exc:
            return WorkerImageArchiveBuildResult(
                ok=False,
                image_id=payload.image_id,
                error_message=f"image build architecture unavailable: {exc}",
            )

        attempted: list[str] = []
        for driver in self._storage_drivers():
            attempted.append(driver.value)
            try:
                return self._build_with_driver(
                    payload,
                    container_id=container_id,
                    driver=driver,
                    registry_auth=registry_auth,
                    build_args=build_args or {},
                    log=log,
                )
            except Exception as exc:
                log(f"buildah {driver.value} build failed: {type(exc).__name__}: {exc}")
                if driver is self._storage_drivers()[-1]:
                    return WorkerImageArchiveBuildResult(
                        ok=False,
                        image_id=payload.image_id,
                        error_message=(
                            "image build failed with storage drivers "
                            f"{', '.join(attempted)}: {type(exc).__name__}: {exc}"
                        ),
                    )
                log(f"retrying image build with {self.fallback_storage_driver.value} storage")
        return WorkerImageArchiveBuildResult(
            ok=False,
            image_id=payload.image_id,
            error_message="image build did not run",
        )

    def _build_with_driver(
        self,
        payload: WorkerImageBuildRequestPayload,
        *,
        container_id: str,
        driver: BuildahStorageDriver,
        registry_auth: ImageBuildRegistryAuth | None,
        build_args: dict[str, str],
        log: ImageBuildLog,
    ) -> WorkerImageArchiveBuildResult:
        lease = self.scratch.acquire(build_id=payload.build_id, container_id=container_id)
        directories = plan_buildah_directories(str(lease.root))
        root = Path(directories.root)
        graphroot = Path(directories.graphroot)
        runroot = Path(directories.runroot)
        tmpdir = Path(directories.tmpdir)
        context_dir = root / "context"
        image_ref = f"image-build:{payload.image_id}"
        build_container_name = _safe_name(f"image-build-{payload.image_id}-{container_id}")
        env: dict[str, str] | None = None
        try:
            for directory in (graphroot, runroot, tmpdir, context_dir):
                directory.mkdir(parents=True, exist_ok=True)

            storage = plan_buildah_storage_config(directories, driver=driver)
            storage_conf = root / "storage.conf"
            storage_conf.write_text(storage.text, encoding="utf-8")
            env = _buildah_env(
                plan_buildah_environment(
                    runroot=directories.runroot,
                    tmpdir=directories.tmpdir,
                    storage_conf_path=str(storage_conf),
                    cpu_count=os.cpu_count() or 1,
                    base_env=_base_env(),
                ).env,
            )
            registry_auth_file = _write_registry_auth_file(root, registry_auth)
            if registry_auth_file is not None:
                env["REGISTRY_AUTH_FILE"] = str(registry_auth_file)
            build_arg_file = _write_build_arg_file(root, build_args)
            self._prepare_context(
                payload,
                context_dir,
                container_id=container_id,
                log=log,
            )
            if payload.build_options.dockerfile:
                dockerfile_path = context_dir / "Dockerfile"
                dockerfile = payload.build_options.dockerfile
                dockerfile_path.write_text(dockerfile, encoding="utf-8")
                log(f"building image {payload.image_id} with buildah {driver.value}")
                build_command = [
                    "bud",
                    "--arch",
                    payload.build_options.architecture.value,
                    "--layers",
                    "--format",
                    "docker",
                    "--file",
                    str(dockerfile_path),
                    "--tag",
                    image_ref,
                ]
                if build_arg_file is not None:
                    build_command.extend(["--build-arg-file", str(build_arg_file)])
                build_command.append(str(context_dir))
                self._run_buildah(
                    build_command,
                    directories=directories,
                    driver=driver,
                    env=env,
                    cwd=context_dir,
                    log=log,
                    scratch=lease,
                )
                self._run_buildah(
                    [
                        "from",
                        "--arch",
                        payload.build_options.architecture.value,
                        "--name",
                        build_container_name,
                        image_ref,
                    ],
                    directories=directories,
                    driver=driver,
                    env=env,
                    cwd=context_dir,
                    log=log,
                    scratch=lease,
                )
            elif payload.build_options.source_image:
                source_image = payload.build_options.source_image
                log(f"pulling source image {source_image} with buildah {driver.value}")
                self._run_buildah(
                    [
                        "from",
                        "--arch",
                        payload.build_options.architecture.value,
                        "--pull-always",
                        "--name",
                        build_container_name,
                        source_image,
                    ],
                    directories=directories,
                    driver=driver,
                    env=env,
                    cwd=context_dir,
                    log=log,
                    scratch=lease,
                )
            else:
                msg = "image build request requires a Dockerfile or source image"
                raise RuntimeError(msg)

            mounted_root = self._run_buildah(
                ["mount", build_container_name],
                directories=directories,
                driver=driver,
                env=env,
                cwd=context_dir,
                log=log,
                capture_stdout=True,
                scratch=lease,
            ).strip()
            if not mounted_root:
                msg = "buildah did not return a mounted root filesystem"
                raise RuntimeError(msg)
            archive = self.archiver.archive_image(
                Path(mounted_root),
                payload.image_id,
                lambda progress: log(f"archive progress: {progress}%"),
            )
            if not archive.success:
                raise RuntimeError(archive.error_message or "archive creation failed")
            log(f"image archive ready: {archive.archive_path}")
            return WorkerImageArchiveBuildResult(
                ok=True,
                image_id=payload.image_id,
                archive_path=archive.archive_path,
            )
        finally:
            try:
                cleanup_failures = self.scratch.cleanup_store(
                    root,
                    driver=driver,
                    env=env,
                )
                for failure in cleanup_failures:
                    log(f"image build scratch cleanup warning: {failure}")
            finally:
                try:
                    log(f"image build scratch peak: {lease.peak_bytes} bytes")
                finally:
                    self.scratch.release(lease)

    def _storage_drivers(self) -> tuple[BuildahStorageDriver, ...]:
        if self.storage_driver is self.fallback_storage_driver:
            return (self.storage_driver,)
        return (self.storage_driver, self.fallback_storage_driver)

    def _prepare_context(
        self,
        payload: WorkerImageBuildRequestPayload,
        context_dir: Path,
        *,
        container_id: str,
        log: ImageBuildLog,
    ) -> None:
        object_id = payload.build_options.build_context_object
        if object_id:
            if self.context_loader is None:
                msg = f"image build context loader is not configured: {object_id}"
                raise RuntimeError(msg)
            loaded = self.context_loader.extract_build_context(
                object_id,
                context_dir,
                workspace_id=payload.workspace_id,
                build_id=payload.build_id,
                container_id=container_id,
            )
            if not loaded.ok:
                msg = (
                    loaded.error_message or f"image build context could not be loaded: {object_id}"
                )
                raise RuntimeError(msg)
            log(f"image build context extracted: {object_id} ({len(loaded.files)} files)")
            return

        requested = payload.build_options.build_context_path
        if not requested:
            return
        source = Path(requested)
        if source.exists() and source.is_dir():
            shutil.copytree(source, context_dir, dirs_exist_ok=True, symlinks=True)
            return
        if payload.build_options.build_context_digest:
            msg = (
                "image build context is not available on this worker: "
                f"{payload.build_options.build_context_path}"
            )
            raise FileNotFoundError(msg)

    def _run_buildah(
        self,
        args: Sequence[str],
        *,
        directories: BuildahDirectoryPlan,
        driver: BuildahStorageDriver,
        env: dict[str, str],
        cwd: Path,
        log: ImageBuildLog,
        capture_stdout: bool = False,
        scratch: ImageBuildScratchLease | None = None,
    ) -> str:
        command = _buildah_command(
            self.buildah_binary,
            args,
            graphroot=directories.graphroot,
            runroot=directories.runroot,
            driver=driver,
        )
        if not capture_stdout:
            if scratch is not None:
                scratch.check_capacity()
            output = _run_logged_process(
                command,
                cwd=cwd,
                env=env,
                log=log,
                capacity_check=scratch.check_capacity if scratch is not None else None,
            )
            if scratch is not None:
                scratch.check_capacity()
            return output
        if scratch is not None:
            scratch.check_capacity()
        process = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if scratch is not None:
            scratch.check_capacity()
        for line in _output_lines(process.stdout, process.stderr):
            log(line)
        if process.returncode != 0:
            msg = _process_error_message(command, process)
            raise RuntimeError(msg)
        return process.stdout if capture_stdout else ""


@dataclass(slots=True)
class RepositoryWorkerImageArchivePublisher:
    repository: ImageArchiveUploadCredentialClient
    content_type: str = "application/x-tar"

    def publish_image_archive(
        self,
        *,
        image_id: str,
        build_id: str = "",
        container_id: str = "",
        upload_capability: str = "",
        archive_path: Path,
        workspace_id: str = "",
        stub_id: str = "",
    ) -> WorkerImageArchivePublishResult:
        if not workspace_id:
            return WorkerImageArchivePublishResult(
                ok=False,
                image_id=image_id,
                error_message="workspace id is required for image archive upload",
            )
        if not archive_path.exists() or not archive_path.is_file():
            return WorkerImageArchivePublishResult(
                ok=False,
                image_id=image_id,
                error_message=f"image archive file not found: {archive_path}",
            )
        size_bytes, archive_sha256 = image_archive_file_identity(archive_path)
        response = self.repository.get_image_archive_upload_credentials(
            ImageArchiveUploadCredentialRequest(
                workspace_id=workspace_id,
                stub_id=stub_id,
                build_id=build_id,
                container_id=container_id,
                image_id=image_id,
                upload_capability=upload_capability,
                archive_size_bytes=size_bytes,
                archive_sha256=archive_sha256,
                content_type=self.content_type,
            )
        )
        credentials = response.credentials
        if credentials is None:
            return WorkerImageArchivePublishResult(
                ok=False,
                image_id=image_id,
                error_message="image archive upload credentials were not returned",
            )
        if not credentials.ok:
            return WorkerImageArchivePublishResult(
                ok=False,
                image_id=image_id,
                object_key=credentials.object_key,
                bucket=credentials.bucket,
                error_message=credentials.error_msg,
            )
        if not credentials.object_key:
            return WorkerImageArchivePublishResult(
                ok=False,
                image_id=image_id,
                bucket=credentials.bucket,
                error_message="image archive location was not returned",
            )
        if not credentials.upload_url:
            # Another build already published this image id. Its bytes are the ones
            # every authorized workspace resolves, so this build adopts that identity
            # rather than uploading a second copy over it.
            return WorkerImageArchivePublishResult(
                ok=True,
                image_id=image_id,
                object_key=credentials.object_key,
                bucket=credentials.bucket,
                size_bytes=credentials.archive_size_bytes,
                sha256=credentials.archive_sha256,
            )
        try:
            upload_image_archive(
                credentials.upload_url,
                archive_path,
                headers=credentials.upload_headers,
                archive_size_bytes=size_bytes,
                archive_sha256=archive_sha256,
                content_type=self.content_type,
                timeout_seconds=IMAGE_ARCHIVE_UPLOAD_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return WorkerImageArchivePublishResult(
                ok=False,
                image_id=image_id,
                object_key=credentials.object_key,
                bucket=credentials.bucket,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        return WorkerImageArchivePublishResult(
            ok=True,
            image_id=image_id,
            object_key=credentials.object_key,
            bucket=credentials.bucket,
            size_bytes=size_bytes,
            sha256=archive_sha256,
        )


def _request_context(
    request: SchedulerWorkerRequest,
    payload: WorkerImageBuildRequestPayload,
) -> ContainerRequestContext:
    return ContainerRequestContext(
        container_id=request.container_id,
        image_id=payload.image_id,
        stub_id=request.stub_id,
        stub_type=IMAGE_BUILD_REQUEST_KIND,
        workspace_id=request.workspace_id,
        env=list(payload.env),
        cpu_millicores=request.cpu_millicores,
        memory_mib=request.memory_mib,
        gpu=request.gpu_type,
        gpu_count=request.gpu_count,
    )


def _extract_zip_context(archive_path: Path, target_dir: Path) -> list[str]:
    target_root = target_dir.resolve()
    files: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_IMAGE_BUILD_CONTEXT_MEMBERS:
            raise ValueError(
                "build context archive exceeds maximum member count of "
                f"{MAX_IMAGE_BUILD_CONTEXT_MEMBERS}"
            )
        planned: list[tuple[zipfile.ZipInfo, Path]] = []
        total_bytes = 0
        seen: set[str] = set()
        for member in members:
            if member.is_dir():
                continue
            relative = _safe_zip_member_path(member.filename)
            if member.flag_bits & 0x1:
                raise ValueError(
                    f"encrypted build context member is not allowed: {member.filename}"
                )
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"symlink build context member is not allowed: {member.filename}")
            if member.file_size > MAX_IMAGE_BUILD_CONTEXT_MEMBER_BYTES:
                raise ValueError(
                    f"build context member exceeds {MAX_IMAGE_BUILD_CONTEXT_MEMBER_BYTES} bytes: "
                    f"{member.filename}"
                )
            total_bytes += member.file_size
            if total_bytes > MAX_IMAGE_BUILD_CONTEXT_UNCOMPRESSED_BYTES:
                raise ValueError(
                    "build context archive exceeds maximum uncompressed size of "
                    f"{MAX_IMAGE_BUILD_CONTEXT_UNCOMPRESSED_BYTES} bytes"
                )
            normalized = relative.as_posix()
            if normalized in seen:
                raise ValueError(f"duplicate build context member: {member.filename}")
            seen.add(normalized)
            planned.append((member, relative))

        extracted_bytes = 0
        for member, relative in planned:
            destination = (target_root / relative).resolve()
            if not destination.is_relative_to(target_root):
                msg = f"unsafe build context path: {member.filename}"
                raise ValueError(msg)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as target:
                member_bytes = 0
                while chunk := source.read(1024 * 1024):
                    member_bytes += len(chunk)
                    extracted_bytes += len(chunk)
                    if member_bytes > MAX_IMAGE_BUILD_CONTEXT_MEMBER_BYTES:
                        raise ValueError(
                            f"build context member expanded beyond limit: {member.filename}"
                        )
                    if extracted_bytes > MAX_IMAGE_BUILD_CONTEXT_UNCOMPRESSED_BYTES:
                        raise ValueError("build context archive expanded beyond total size limit")
                    target.write(chunk)
            mode = (member.external_attr >> 16) & 0o777
            if mode:
                destination.chmod(mode)
            files.append(relative.as_posix())
    return files


def _safe_zip_member_path(filename: str) -> Path:
    if len(filename) > MAX_IMAGE_BUILD_CONTEXT_PATH_LENGTH:
        raise ValueError(f"build context path exceeds maximum length: {filename[:80]}")
    clean = posixpath.normpath(filename.replace("\\", "/"))
    if clean in {"", "."} or clean.startswith("../") or clean.startswith("/"):
        msg = f"unsafe build context path: {filename}"
        raise ValueError(msg)
    parts = tuple(part for part in clean.split("/") if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        msg = f"unsafe build context path: {filename}"
        raise ValueError(msg)
    if len(parts) > MAX_IMAGE_BUILD_CONTEXT_PATH_DEPTH:
        raise ValueError(f"build context path exceeds maximum depth: {filename}")
    return Path(*parts)


def _base_env() -> list[str]:
    values = dict(os.environ)
    registry_auth_file = values.get("REGISTRY_AUTH_FILE")
    if registry_auth_file and not Path(registry_auth_file).exists():
        values.pop("REGISTRY_AUTH_FILE", None)
    return [f"{key}={value}" for key, value in values.items()]


def _write_registry_auth_file(
    root: Path, registry_auth: ImageBuildRegistryAuth | None
) -> Path | None:
    if registry_auth is None:
        return None
    entry = (
        {"auth": registry_auth.auth}
        if registry_auth.auth
        else {"identitytoken": registry_auth.identity_token}
    )
    path = root / "registry-auth.json"
    _write_private_file(
        path,
        json.dumps({"auths": {registry_auth.registry: entry}}, separators=(",", ":")),
    )
    return path


def _write_build_arg_file(root: Path, build_args: dict[str, str]) -> Path | None:
    if not build_args:
        return None
    lines: list[str] = []
    for name, value in sorted(build_args.items()):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"invalid image build argument name: {name}")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"image build argument must be a single line: {name}")
        lines.append(f"{name}={value}")
    path = root / "build-args.conf"
    _write_private_file(path, "\n".join(lines) + "\n")
    return path


def _write_private_file(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1
            output.write(value)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _image_build_private_values(private_inputs: ImageBuildPrivateInputs) -> tuple[str, ...]:
    values = {value for value in private_inputs.build_args.values() if value}
    if private_inputs.registry_auth is not None:
        values.update(
            value
            for value in (
                private_inputs.registry_auth.auth,
                private_inputs.registry_auth.identity_token,
            )
            if value
        )
    return tuple(sorted(values, key=len, reverse=True))


def _redact_image_build_text(value: str, *, sensitive_values: tuple[str, ...]) -> str:
    result = value
    for secret in sensitive_values:
        if secret:
            result = result.replace(secret, "<redacted>")
    return result


def _buildah_env(values: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if separator and key:
            env[key] = value
    return env


def _run_logged_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log: ImageBuildLog,
    heartbeat_seconds: float = 15.0,
    capacity_check: Callable[[], None] | None = None,
) -> str:
    lines: list[str] = []
    started = time.monotonic()
    last_output = started
    stop_heartbeat = threading.Event()
    capacity_failures: list[Exception] = []

    def heartbeat() -> None:
        nonlocal last_output
        while not stop_heartbeat.wait(max(heartbeat_seconds, 0.1)):
            if time.monotonic() - last_output >= heartbeat_seconds:
                elapsed_seconds = int(time.monotonic() - started)
                log(f"still running {command[0]} {command[-1]} ({elapsed_seconds}s elapsed)")
                last_output = time.monotonic()

    read_fd, write_fd = os.pipe()
    try:
        process: subprocess.Popen[bytes] = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=write_fd,
            stderr=write_fd,
            close_fds=True,
        )
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    os.close(write_fd)

    def monitor_capacity() -> None:
        if capacity_check is None:
            return
        while not stop_heartbeat.wait(IMAGE_BUILD_SCRATCH_MONITOR_SECONDS):
            try:
                capacity_check()
            except Exception as exc:
                capacity_failures.append(exc)
                process.terminate()
                return

    heartbeat_thread = threading.Thread(
        target=heartbeat,
        name=f"buildah-heartbeat-{command[0]}",
        daemon=True,
    )
    capacity_thread = threading.Thread(
        target=monitor_capacity,
        name=f"buildah-capacity-{command[0]}",
        daemon=True,
    )
    heartbeat_thread.start()
    capacity_thread.start()
    try:
        with os.fdopen(read_fd, "r", encoding="utf-8", errors="replace") as output:
            for raw_line in output:
                for line in _output_lines(raw_line):
                    lines.append(line)
                    last_output = time.monotonic()
                    log(line)
        return_code = process.wait()
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=1.0)
        capacity_thread.join(timeout=1.0)
    if capacity_failures:
        raise capacity_failures[0]
    if return_code != 0:
        raise RuntimeError(_command_error_message(command, return_code, "\n".join(lines)))
    return ""


def _buildah_command(
    binary: str,
    args: Sequence[str],
    *,
    graphroot: str,
    runroot: str,
    driver: BuildahStorageDriver,
) -> list[str]:
    return [
        binary,
        "--root",
        graphroot,
        "--runroot",
        runroot,
        "--storage-driver",
        driver.value,
        *args,
    ]


def _safe_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return (clean or "image-build")[:100]


def _output_lines(*values: str) -> list[str]:
    return [line for value in values for line in value.splitlines() if line.strip()]


def _process_error_message(
    command: Sequence[str],
    process: subprocess.CompletedProcess[str],
) -> str:
    stderr = process.stderr.strip()
    stdout = process.stdout.strip()
    detail = stderr or stdout
    if detail:
        return f"{command[0]} exited {process.returncode}: {detail}"
    return f"{command[0]} exited {process.returncode}"


def _command_error_message(command: Sequence[str], return_code: int, output: str) -> str:
    detail = output.strip()
    if detail:
        return f"{command[0]} exited {return_code}: {detail}"
    return f"{command[0]} exited {return_code}"


def _append_unique_log(logs: list[str], message: str) -> list[str]:
    clean = message.rstrip()
    if not clean or (logs and logs[-1] == clean):
        return logs
    return [*logs, clean]
