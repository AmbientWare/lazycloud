from __future__ import annotations

import hashlib
import posixpath
from enum import StrEnum

from pydantic import Field, JsonValue
from shared.app_identity import CHECKPOINT_SIGNAL_ROOT
from shared.contracts import ContractModel

from worker import execution
from worker.execution import (
    CHECKPOINT_ARCHIVE_EXTENSION,
    CHECKPOINT_FILESYSTEM_DIR,
    CHECKPOINT_ORIGIN_PREFIX,
    CheckpointCacheMetadata,
    OciMount,
    checkpoint_accelerator,
)

DEFAULT_CHECKPOINT_SIGNAL_ROOT = CHECKPOINT_SIGNAL_ROOT
DEFAULT_CHECKPOINT_WORK_ROOT = "/tmp"
DEFAULT_CHECKPOINT_DEADLINE_SECONDS = 10 * 60
DEFAULT_CHECKPOINT_POLL_SECONDS = 1
MIN_NVIDIA_CRIU_DRIVER_VERSION = 570
CHECKPOINT_READY_LOG_RATE = 10
CHECKPOINT_SIGNAL_MOUNT_PATH = "/criu"
CHECKPOINT_SIGNAL_FILE_NAME = "READY_FOR_CHECKPOINT"
CHECKPOINT_COMPLETE_FILE_NAME = "CHECKPOINT_COMPLETE"
CHECKPOINT_CONTAINER_ID_FILE_NAME = "CONTAINER_ID"
CHECKPOINT_CONTAINER_HOSTNAME_FILE_NAME = "CONTAINER_HOSTNAME"


class WorkerCheckpointStatus(StrEnum):
    Pending = "pending"
    Available = "available"
    CheckpointFailed = "checkpoint-failed"
    RestoreFailed = "restore-failed"


class CheckpointMode(StrEnum):
    Nvidia = "nvidia"


class CheckpointLifecycleAction(StrEnum):
    Skip = "skip"
    Create = "create"
    Restore = "restore"
    Materialize = "materialize"
    FallbackRun = "fallback-run"
    Fail = "fail"


class CheckpointArchiveValidationStatus(StrEnum):
    Valid = "valid"
    IncompleteMetadata = "incomplete-metadata"
    SizeMismatch = "size-mismatch"
    HashMismatch = "hash-mismatch"


class CheckpointStateOperation(StrEnum):
    Create = "create"
    UpdateStatus = "update-status"
    MarkRestored = "mark-restored"


class CheckpointPersistenceAction(StrEnum):
    Persist = "persist"
    Reject = "reject"


class CheckpointMaterializationAction(StrEnum):
    ReuseMaterialized = "reuse-materialized"
    RestoreFromCache = "restore-from-cache"
    DownloadFromOrigin = "download-from-origin"
    Reject = "reject"


class CheckpointArchiveSource(StrEnum):
    LocalMaterialized = "local-materialized"
    Cache = "cache"
    OriginStorage = "origin-storage"


class CheckpointSignalWaitAction(StrEnum):
    Ready = "ready"
    Wait = "wait"
    Timeout = "timeout"


class CheckpointAvailabilityRequest(ContractModel):
    runtime_checkpoint_restore: bool = True
    manager_initialized: bool = True
    manager_available: bool = True
    pool_name: str = ""
    pool_criu_enabled: bool = False


class CheckpointAvailabilityDecision(ContractModel):
    runtime_supported: bool
    criu_available: bool
    supports_checkpoint: bool
    reason: str


class CheckpointLifecycleDecision(ContractModel):
    action: CheckpointLifecycleAction
    should_run_container: bool = False
    should_disable_checkpoint: bool = False
    state_status: WorkerCheckpointStatus | None = None
    reason: str = ""
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class CheckpointSignalPlan(ContractModel):
    container_id: str
    signal_dir: str
    ready_file: str
    complete_file: str
    container_id_file: str
    container_hostname_file: str
    mount: OciMount
    file_writes: dict[str, str] = Field(default_factory=dict)


class CheckpointRestoreRequest(ContractModel):
    checkpoint_id: str = ""
    checkpoint_status: WorkerCheckpointStatus | str = WorkerCheckpointStatus.Pending
    supports_checkpoint: bool = True
    materialized: bool = False
    has_complete_metadata: bool = True
    stub_is_deployment: bool = True

    @property
    def normalized_status(self) -> WorkerCheckpointStatus | None:
        if isinstance(self.checkpoint_status, WorkerCheckpointStatus):
            return self.checkpoint_status
        try:
            return WorkerCheckpointStatus(self.checkpoint_status)
        except ValueError:
            return None


class CheckpointStatePayload(ContractModel):
    operation: CheckpointStateOperation
    checkpoint_id: str
    status: WorkerCheckpointStatus | None = None
    source_container_id: str = ""
    container_ip: str = ""
    remote_key: str = ""
    stub_id: str = ""
    stub_type: str = ""
    workspace_id: str = ""
    app_id: str = ""
    exposed_ports: list[int] = Field(default_factory=list)
    cache_hash: str = ""
    cache_size_bytes: int = 0
    origin_key: str = ""
    locality: str = ""
    accelerator: str = ""
    update_last_restored_at: bool = False


class CheckpointArchiveValidation(ContractModel):
    status: CheckpointArchiveValidationStatus
    ok: bool
    expected_hash: str = ""
    actual_hash: str = ""
    expected_size_bytes: int = 0
    actual_size_bytes: int = 0
    reason: str = ""


class CheckpointPersistenceRequest(ContractModel):
    checkpoint_id: str
    checkpoint_root: str
    origin_storage_available: bool
    content_cache_available: bool
    cache_hash: str = ""
    cache_size_bytes: int = 0
    locality: str = ""
    gpu: str | None = None
    accelerator: str = ""


class CheckpointPersistencePlan(ContractModel):
    action: CheckpointPersistenceAction
    checkpoint_id: str
    checkpoint_path: str = ""
    archive_path: str = ""
    origin_key: str = ""
    create_archive: bool = False
    upload_to_origin_storage: bool = False
    store_archive_in_cache: bool = False
    remove_existing_archive: bool = False
    cleanup_archive_after_persist: bool = False
    metadata: CheckpointCacheMetadata | None = None
    error_message: str = ""


class CheckpointArchiveMaterializationRequest(ContractModel):
    checkpoint_id: str
    checkpoint_root: str
    cache_hash: str = ""
    cache_size_bytes: int = 0
    origin_key: str = ""
    materialized: bool = False
    cache_available: bool = True
    origin_storage_available: bool = True
    locality: str = ""
    accelerator: str = ""
    gpu: str | None = None


class CheckpointArchiveMaterializationPlan(ContractModel):
    action: CheckpointMaterializationAction
    checkpoint_id: str
    checkpoint_path: str
    archive_path: str
    filesystem_payload_path: str
    temporary_extract_root: str
    source_order: list[CheckpointArchiveSource] = Field(default_factory=list)
    origin_key: str = ""
    cache_hash: str = ""
    expected_size_bytes: int = 0
    validate_archive: bool = False
    remove_archive_after_materialize: bool = False
    store_download_in_cache: bool = False
    error_message: str = ""


class CheckpointSignalWaitPlan(ContractModel):
    action: CheckpointSignalWaitAction
    container_id: str
    ready_file: str
    deadline_seconds: int = DEFAULT_CHECKPOINT_DEADLINE_SECONDS
    poll_seconds: int = DEFAULT_CHECKPOINT_POLL_SECONDS
    log_sample_rate: int = CHECKPOINT_READY_LOG_RATE
    reason: str


class CriuCheckpointOptions(ContractModel):
    leave_running: bool = True
    allow_open_tcp: bool = True
    skip_in_flight: bool = True
    link_remap: bool = True

    def as_option_map(self) -> dict[str, JsonValue]:
        options: dict[str, JsonValue] = {
            "leave_running": self.leave_running,
            "allow_open_tcp": self.allow_open_tcp,
            "skip_in_flight": self.skip_in_flight,
            "link_remap": self.link_remap,
        }
        return options


class CriuRestoreOptions(ContractModel):
    tcp_close: bool = True

    def as_option_map(self) -> dict[str, JsonValue]:
        options: dict[str, JsonValue] = {"tcp_close": self.tcp_close}
        return options


class CheckpointRequest(ContractModel):
    container_id: str
    checkpoint_id: str
    checkpoint_root: str
    runtime_name: str = "runc"
    mode: CheckpointMode = CheckpointMode.Nvidia
    config_path: str = ""
    gpu_count: int = 0
    nvidia_driver_major: int | None = None
    leave_running: bool = True
    allow_open_tcp: bool = True
    skip_in_flight: bool = True
    link_remap: bool = True


class CheckpointPlan(ContractModel):
    request: CheckpointRequest
    available: bool
    checkpoint_path: str
    work_dir: str
    create_work_dir: bool = True
    checkpoint_options: CriuCheckpointOptions = Field(default_factory=CriuCheckpointOptions)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    reason: str = ""


class RestorePlan(ContractModel):
    request: CheckpointRequest
    available: bool
    image_path: str
    work_dir: str
    bundle_path: str
    create_work_dir: bool = True
    restore_options: CriuRestoreOptions = Field(default_factory=CriuRestoreOptions)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    reason: str = ""


def plan_checkpoint_availability(
    request: CheckpointAvailabilityRequest,
) -> CheckpointAvailabilityDecision:
    if not request.runtime_checkpoint_restore:
        return CheckpointAvailabilityDecision(
            runtime_supported=False,
            criu_available=False,
            supports_checkpoint=False,
            reason="runtime does not support checkpoint/restore",
        )
    if not request.manager_initialized:
        return CheckpointAvailabilityDecision(
            runtime_supported=True,
            criu_available=False,
            supports_checkpoint=False,
            reason="CRIU manager is not initialized",
        )
    if not request.manager_available:
        return CheckpointAvailabilityDecision(
            runtime_supported=True,
            criu_available=False,
            supports_checkpoint=False,
            reason="CRIU manager is not available",
        )
    if not request.pool_name:
        return CheckpointAvailabilityDecision(
            runtime_supported=True,
            criu_available=False,
            supports_checkpoint=False,
            reason="worker pool name is not set",
        )
    if not request.pool_criu_enabled:
        return CheckpointAvailabilityDecision(
            runtime_supported=True,
            criu_available=False,
            supports_checkpoint=False,
            reason=f"worker pool {request.pool_name!r} does not enable checkpointing",
        )
    return CheckpointAvailabilityDecision(
        runtime_supported=True,
        criu_available=True,
        supports_checkpoint=True,
        reason="runtime and worker pool support checkpoint/restore",
    )


def plan_auto_checkpoint(
    *,
    supports_checkpoint: bool,
    checkpoint_enabled: bool,
    existing_checkpoint_id: str = "",
) -> CheckpointLifecycleDecision:
    if not supports_checkpoint:
        return CheckpointLifecycleDecision(
            action=CheckpointLifecycleAction.Skip,
            reason="checkpoint/restore is unavailable",
        )
    if not checkpoint_enabled:
        return CheckpointLifecycleDecision(
            action=CheckpointLifecycleAction.Skip,
            reason="checkpointing is disabled for this request",
        )
    if existing_checkpoint_id:
        return CheckpointLifecycleDecision(
            action=CheckpointLifecycleAction.Skip,
            reason="request already has a checkpoint to restore",
            metadata={"checkpoint_id": existing_checkpoint_id},
        )
    return CheckpointLifecycleDecision(
        action=CheckpointLifecycleAction.Create,
        reason="request is eligible for automatic checkpoint creation",
    )


def checkpoint_signal_dir(
    container_id: str,
    *,
    root: str = DEFAULT_CHECKPOINT_SIGNAL_ROOT,
) -> str:
    if not container_id:
        msg = "container_id is required"
        raise ValueError(msg)
    return posixpath.join(root.rstrip("/"), container_id, "criu")


def plan_checkpoint_signal_mount(
    *,
    container_id: str,
    container_hostname: str,
    root: str = DEFAULT_CHECKPOINT_SIGNAL_ROOT,
) -> CheckpointSignalPlan:
    signal_dir = checkpoint_signal_dir(container_id, root=root)
    return CheckpointSignalPlan(
        container_id=container_id,
        signal_dir=signal_dir,
        ready_file=posixpath.join(signal_dir, CHECKPOINT_SIGNAL_FILE_NAME),
        complete_file=posixpath.join(signal_dir, CHECKPOINT_COMPLETE_FILE_NAME),
        container_id_file=posixpath.join(signal_dir, CHECKPOINT_CONTAINER_ID_FILE_NAME),
        container_hostname_file=posixpath.join(
            signal_dir,
            CHECKPOINT_CONTAINER_HOSTNAME_FILE_NAME,
        ),
        mount=OciMount(
            source=signal_dir,
            destination=CHECKPOINT_SIGNAL_MOUNT_PATH,
            options=["rbind", "rprivate", "nosuid", "nodev"],
        ),
        file_writes={
            CHECKPOINT_CONTAINER_ID_FILE_NAME: container_id,
            CHECKPOINT_CONTAINER_HOSTNAME_FILE_NAME: container_hostname,
        },
    )


def plan_checkpoint_restore(
    request: CheckpointRestoreRequest,
) -> CheckpointLifecycleDecision:
    if not request.supports_checkpoint:
        return CheckpointLifecycleDecision(
            action=CheckpointLifecycleAction.Skip,
            should_run_container=True,
            reason="checkpoint/restore is unavailable",
        )
    if not request.checkpoint_id:
        return CheckpointLifecycleDecision(
            action=CheckpointLifecycleAction.Skip,
            should_run_container=True,
            reason="request has no checkpoint",
        )
    if request.normalized_status is not WorkerCheckpointStatus.Available:
        return CheckpointLifecycleDecision(
            action=CheckpointLifecycleAction.Fail,
            state_status=WorkerCheckpointStatus.RestoreFailed,
            reason="checkpoint is not available",
            metadata={"checkpoint_id": request.checkpoint_id},
        )
    if not request.has_complete_metadata:
        return CheckpointLifecycleDecision(
            action=CheckpointLifecycleAction.Fail,
            state_status=WorkerCheckpointStatus.RestoreFailed,
            reason="checkpoint cache metadata is incomplete",
            metadata={"checkpoint_id": request.checkpoint_id},
        )
    if not request.materialized:
        return CheckpointLifecycleDecision(
            action=CheckpointLifecycleAction.Materialize,
            reason="checkpoint archive must be materialized before restore",
            metadata={"checkpoint_id": request.checkpoint_id},
        )
    return CheckpointLifecycleDecision(
        action=CheckpointLifecycleAction.Restore,
        reason="checkpoint is available and materialized",
        metadata={"checkpoint_id": request.checkpoint_id},
    )


def plan_checkpoint_restore_result(
    *,
    restored: bool,
    stub_is_deployment: bool,
    error_message: str = "",
) -> CheckpointLifecycleDecision:
    if restored:
        return CheckpointLifecycleDecision(
            action=CheckpointLifecycleAction.Skip,
            reason="checkpoint restored successfully",
        )
    if stub_is_deployment:
        return CheckpointLifecycleDecision(
            action=CheckpointLifecycleAction.FallbackRun,
            should_run_container=True,
            should_disable_checkpoint=True,
            state_status=WorkerCheckpointStatus.RestoreFailed if error_message else None,
            reason="deployment restore failed; run container without checkpoint",
        )
    return CheckpointLifecycleDecision(
        action=CheckpointLifecycleAction.Fail,
        should_run_container=False,
        state_status=WorkerCheckpointStatus.RestoreFailed,
        reason="restore failed and non-deployment requests cannot fall back",
        metadata={"error_message": error_message},
    )


def create_checkpoint_state_payload(
    *,
    checkpoint_id: str,
    source_container_id: str,
    status: WorkerCheckpointStatus,
    container_ip: str = "",
    stub_id: str = "",
    stub_type: str = "",
    workspace_id: str = "",
    app_id: str = "",
    exposed_ports: list[int] | None = None,
    cache_hash: str = "",
    cache_size_bytes: int = 0,
    origin_key: str = "",
    locality: str = "",
    accelerator: str = "",
) -> CheckpointStatePayload:
    return CheckpointStatePayload(
        operation=CheckpointStateOperation.Create,
        checkpoint_id=checkpoint_id,
        source_container_id=source_container_id,
        container_ip=container_ip,
        status=status,
        remote_key=checkpoint_id,
        stub_id=stub_id,
        stub_type=stub_type,
        workspace_id=workspace_id,
        app_id=app_id,
        exposed_ports=list(exposed_ports or []),
        cache_hash=cache_hash,
        cache_size_bytes=cache_size_bytes,
        origin_key=origin_key,
        locality=locality,
        accelerator=accelerator,
    )


def update_checkpoint_status_payload(
    checkpoint_id: str,
    status: WorkerCheckpointStatus,
) -> CheckpointStatePayload:
    return CheckpointStatePayload(
        operation=CheckpointStateOperation.UpdateStatus,
        checkpoint_id=checkpoint_id,
        status=status,
    )


def mark_checkpoint_restored_payload(checkpoint_id: str) -> CheckpointStatePayload:
    return CheckpointStatePayload(
        operation=CheckpointStateOperation.MarkRestored,
        checkpoint_id=checkpoint_id,
        status=WorkerCheckpointStatus.Available,
        update_last_restored_at=True,
    )


def checkpoint_origin_key(checkpoint_id: str) -> str:
    return posixpath.join(CHECKPOINT_ORIGIN_PREFIX, checkpoint_id + CHECKPOINT_ARCHIVE_EXTENSION)


def checkpoint_archive_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_checkpoint_archive(
    *,
    expected_hash: str,
    expected_size_bytes: int,
    actual_hash: str,
    actual_size_bytes: int,
) -> CheckpointArchiveValidation:
    if not expected_hash or expected_size_bytes <= 0:
        return CheckpointArchiveValidation(
            status=CheckpointArchiveValidationStatus.IncompleteMetadata,
            ok=False,
            expected_hash=expected_hash,
            expected_size_bytes=expected_size_bytes,
            actual_hash=actual_hash,
            actual_size_bytes=actual_size_bytes,
            reason="checkpoint cache metadata is incomplete",
        )
    if actual_size_bytes != expected_size_bytes:
        return CheckpointArchiveValidation(
            status=CheckpointArchiveValidationStatus.SizeMismatch,
            ok=False,
            expected_hash=expected_hash,
            expected_size_bytes=expected_size_bytes,
            actual_hash=actual_hash,
            actual_size_bytes=actual_size_bytes,
            reason="checkpoint archive size mismatch",
        )
    if actual_hash != expected_hash:
        return CheckpointArchiveValidation(
            status=CheckpointArchiveValidationStatus.HashMismatch,
            ok=False,
            expected_hash=expected_hash,
            expected_size_bytes=expected_size_bytes,
            actual_hash=actual_hash,
            actual_size_bytes=actual_size_bytes,
            reason="checkpoint archive hash mismatch",
        )
    return CheckpointArchiveValidation(
        status=CheckpointArchiveValidationStatus.Valid,
        ok=True,
        expected_hash=expected_hash,
        expected_size_bytes=expected_size_bytes,
        actual_hash=actual_hash,
        actual_size_bytes=actual_size_bytes,
        reason="checkpoint archive matches expected metadata",
    )


def checkpoint_materialized_from_entries(entries: set[str], checkpoint_path: str) -> bool:
    return posixpath.join(checkpoint_path.rstrip("/"), CHECKPOINT_FILESYSTEM_DIR) in entries


def checkpoint_cache_metadata_from_record(
    *,
    checkpoint_id: str,
    cache_hash: str,
    cache_size_bytes: int,
    origin_key: str = "",
    locality: str = "",
    accelerator: str = "",
    gpu: str | None = None,
) -> CheckpointCacheMetadata:
    return CheckpointCacheMetadata(
        cache_hash=cache_hash,
        size_bytes=cache_size_bytes,
        origin_key=origin_key or checkpoint_origin_key(checkpoint_id),
        locality=locality,
        accelerator=accelerator or checkpoint_accelerator(gpu),
    )


def plan_checkpoint_persistence(
    request: CheckpointPersistenceRequest,
) -> CheckpointPersistencePlan:
    checkpoint_path = execution.checkpoint_path(request.checkpoint_root, request.checkpoint_id)
    archive_path = execution.checkpoint_archive_path(request.checkpoint_root, request.checkpoint_id)
    origin_key = checkpoint_origin_key(request.checkpoint_id)
    if not request.content_cache_available:
        return CheckpointPersistencePlan(
            action=CheckpointPersistenceAction.Reject,
            checkpoint_id=request.checkpoint_id,
            checkpoint_path=checkpoint_path,
            archive_path=archive_path,
            origin_key=origin_key,
            error_message="cache is required for checkpoint persistence",
        )
    if not request.origin_storage_available:
        return CheckpointPersistencePlan(
            action=CheckpointPersistenceAction.Reject,
            checkpoint_id=request.checkpoint_id,
            checkpoint_path=checkpoint_path,
            archive_path=archive_path,
            origin_key=origin_key,
            error_message="origin storage is required for checkpoint persistence",
        )

    metadata = None
    if request.cache_hash and request.cache_size_bytes > 0:
        metadata = checkpoint_cache_metadata_from_record(
            checkpoint_id=request.checkpoint_id,
            cache_hash=request.cache_hash,
            cache_size_bytes=request.cache_size_bytes,
            origin_key=origin_key,
            locality=request.locality,
            accelerator=request.accelerator,
            gpu=request.gpu,
        )
    return CheckpointPersistencePlan(
        action=CheckpointPersistenceAction.Persist,
        checkpoint_id=request.checkpoint_id,
        checkpoint_path=checkpoint_path,
        archive_path=archive_path,
        origin_key=origin_key,
        create_archive=True,
        upload_to_origin_storage=True,
        store_archive_in_cache=True,
        remove_existing_archive=True,
        cleanup_archive_after_persist=True,
        metadata=metadata,
    )


def plan_checkpoint_archive_materialization(
    request: CheckpointArchiveMaterializationRequest,
) -> CheckpointArchiveMaterializationPlan:
    checkpoint_path = execution.checkpoint_path(request.checkpoint_root, request.checkpoint_id)
    archive_path = execution.checkpoint_archive_path(request.checkpoint_root, request.checkpoint_id)
    filesystem_payload_path = posixpath.join(checkpoint_path, CHECKPOINT_FILESYSTEM_DIR)
    temporary_extract_root = posixpath.join(
        posixpath.dirname(checkpoint_path),
        f".{request.checkpoint_id}.extract",
    )
    if request.materialized:
        return CheckpointArchiveMaterializationPlan(
            action=CheckpointMaterializationAction.ReuseMaterialized,
            checkpoint_id=request.checkpoint_id,
            checkpoint_path=checkpoint_path,
            archive_path=archive_path,
            filesystem_payload_path=filesystem_payload_path,
            temporary_extract_root=temporary_extract_root,
            source_order=[CheckpointArchiveSource.LocalMaterialized],
        )
    if not request.cache_hash or request.cache_size_bytes <= 0 or not request.origin_key:
        return CheckpointArchiveMaterializationPlan(
            action=CheckpointMaterializationAction.Reject,
            checkpoint_id=request.checkpoint_id,
            checkpoint_path=checkpoint_path,
            archive_path=archive_path,
            filesystem_payload_path=filesystem_payload_path,
            temporary_extract_root=temporary_extract_root,
            cache_hash=request.cache_hash,
            expected_size_bytes=request.cache_size_bytes,
            origin_key=request.origin_key,
            error_message="checkpoint cache metadata is incomplete",
        )

    sources: list[CheckpointArchiveSource] = []
    if request.cache_available:
        sources.append(CheckpointArchiveSource.Cache)
    if request.origin_storage_available:
        sources.append(CheckpointArchiveSource.OriginStorage)
    if not sources:
        return CheckpointArchiveMaterializationPlan(
            action=CheckpointMaterializationAction.Reject,
            checkpoint_id=request.checkpoint_id,
            checkpoint_path=checkpoint_path,
            archive_path=archive_path,
            filesystem_payload_path=filesystem_payload_path,
            temporary_extract_root=temporary_extract_root,
            cache_hash=request.cache_hash,
            expected_size_bytes=request.cache_size_bytes,
            origin_key=request.origin_key,
            error_message="cache and origin storage are unavailable",
        )

    action = (
        CheckpointMaterializationAction.RestoreFromCache
        if CheckpointArchiveSource.Cache in sources
        else CheckpointMaterializationAction.DownloadFromOrigin
    )
    return CheckpointArchiveMaterializationPlan(
        action=action,
        checkpoint_id=request.checkpoint_id,
        checkpoint_path=checkpoint_path,
        archive_path=archive_path,
        filesystem_payload_path=filesystem_payload_path,
        temporary_extract_root=temporary_extract_root,
        source_order=sources,
        origin_key=request.origin_key,
        cache_hash=request.cache_hash,
        expected_size_bytes=request.cache_size_bytes,
        validate_archive=True,
        remove_archive_after_materialize=True,
        store_download_in_cache=(
            action is CheckpointMaterializationAction.DownloadFromOrigin and request.cache_available
        ),
    )


def plan_checkpoint_signal_wait(
    *,
    container_id: str,
    ready_file_exists: bool,
    container_known: bool = True,
    deadline_exceeded: bool = False,
    root: str = DEFAULT_CHECKPOINT_SIGNAL_ROOT,
    deadline_seconds: int = DEFAULT_CHECKPOINT_DEADLINE_SECONDS,
) -> CheckpointSignalWaitPlan:
    ready_file = posixpath.join(
        checkpoint_signal_dir(container_id, root=root),
        CHECKPOINT_SIGNAL_FILE_NAME,
    )
    if deadline_exceeded:
        return CheckpointSignalWaitPlan(
            action=CheckpointSignalWaitAction.Timeout,
            container_id=container_id,
            ready_file=ready_file,
            deadline_seconds=deadline_seconds,
            reason="checkpoint deadline exceeded or container exited",
        )
    if not container_known:
        return CheckpointSignalWaitPlan(
            action=CheckpointSignalWaitAction.Wait,
            container_id=container_id,
            ready_file=ready_file,
            deadline_seconds=deadline_seconds,
            reason="container instance not found yet",
        )
    if ready_file_exists:
        return CheckpointSignalWaitPlan(
            action=CheckpointSignalWaitAction.Ready,
            container_id=container_id,
            ready_file=ready_file,
            deadline_seconds=deadline_seconds,
            reason="container ready for checkpoint",
        )
    return CheckpointSignalWaitPlan(
        action=CheckpointSignalWaitAction.Wait,
        container_id=container_id,
        ready_file=ready_file,
        deadline_seconds=deadline_seconds,
        reason="container not ready for checkpoint",
    )


def nvidia_criu_compatible(gpu_count: int, driver_major: int | None) -> bool:
    if gpu_count <= 0:
        return True
    return driver_major is not None and driver_major >= MIN_NVIDIA_CRIU_DRIVER_VERSION


def nvidia_criu_compatibility_reason(gpu_count: int, driver_major: int | None) -> str:
    if gpu_count <= 0:
        return "checkpoint mode is compatible without GPU driver validation"
    if driver_major is None:
        return "nvidia driver version is unavailable"
    if driver_major < MIN_NVIDIA_CRIU_DRIVER_VERSION:
        return "nvidia driver is below CRIU minimum"
    return "checkpoint mode is compatible"


def build_checkpoint_plan(request: CheckpointRequest) -> CheckpointPlan:
    available = nvidia_criu_compatible(request.gpu_count, request.nvidia_driver_major)
    options = CriuCheckpointOptions(
        leave_running=request.leave_running,
        allow_open_tcp=request.allow_open_tcp,
        skip_in_flight=request.skip_in_flight,
        link_remap=request.link_remap,
    )
    return CheckpointPlan(
        request=request,
        available=available,
        checkpoint_path=execution.checkpoint_path(request.checkpoint_root, request.checkpoint_id),
        work_dir=posixpath.join(DEFAULT_CHECKPOINT_WORK_ROOT, request.checkpoint_id),
        create_work_dir=True,
        checkpoint_options=options,
        options=options.as_option_map(),
        reason=nvidia_criu_compatibility_reason(
            request.gpu_count,
            request.nvidia_driver_major,
        ),
    )


def build_restore_plan(request: CheckpointRequest) -> RestorePlan:
    available = nvidia_criu_compatible(request.gpu_count, request.nvidia_driver_major)
    options = CriuRestoreOptions()
    bundle_path = posixpath.dirname(request.config_path) if request.config_path else ""
    return RestorePlan(
        request=request,
        available=available,
        image_path=execution.checkpoint_path(request.checkpoint_root, request.checkpoint_id),
        work_dir=posixpath.join(DEFAULT_CHECKPOINT_WORK_ROOT, request.checkpoint_id),
        bundle_path=bundle_path,
        create_work_dir=True,
        restore_options=options,
        options=options.as_option_map(),
        reason=nvidia_criu_compatibility_reason(
            request.gpu_count,
            request.nvidia_driver_major,
        ),
    )


def is_criu_restore_error(stderr: str) -> bool:
    normalized = stderr.lower()
    return "criu failed" in normalized and "type restore" in normalized
