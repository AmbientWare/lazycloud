from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from networking.internal_http import InternalHttpClient
from shared.checkpoints import CheckpointRecord
from worker.checkpoints import (
    CheckpointPersistencePlan,
    checkpoint_archive_hash_and_size,
    create_checkpoint_archive,
)
from worker.container_checkpoints import WorkerCheckpointPersistenceResult
from worker.image_archive_cache import WorkerContentCache
from worker.repository_client import WorkerRepositoryHttpClient
from worker.repository_payloads import (
    GetCheckpointRestoreRequest,
    PersistCheckpointArchiveRequest,
    PrepareCheckpointArchiveUploadRequest,
)

PRESIGNED_DOWNLOAD_CHUNK_SIZE_BYTES = 1024 * 1024


@dataclass(slots=True)
class RemoteCheckpointPersister:
    repository: WorkerRepositoryHttpClient
    internal_http: InternalHttpClient
    cache_namespace: str
    cache: WorkerContentCache | None = None

    def persist_checkpoint(
        self,
        plan: CheckpointPersistencePlan,
    ) -> WorkerCheckpointPersistenceResult:
        if plan.error_message:
            raise RuntimeError(plan.error_message)
        archive_path = Path(plan.archive_path)
        checkpoint_path = Path(plan.checkpoint_path)
        if plan.remove_existing_archive:
            archive_path.unlink(missing_ok=True)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        create_checkpoint_archive(checkpoint_path, archive_path, checkpoint_id=plan.checkpoint_id)
        cache_hash, size_bytes = checkpoint_archive_hash_and_size(archive_path)
        try:
            prepared = self.repository.prepare_checkpoint_archive_upload(
                PrepareCheckpointArchiveUploadRequest(
                    checkpoint_id=plan.checkpoint_id,
                    origin_key=plan.origin_key,
                    cache_hash=cache_hash,
                    cache_size_bytes=size_bytes,
                )
            )
            if not prepared.upload_url:
                msg = "checkpoint archive upload URL was not returned"
                raise RuntimeError(msg)
            _put_presigned_checkpoint_archive(
                self.internal_http,
                prepared.upload_url,
                archive_path,
                content_length=size_bytes,
            )
            # Also seed the content cache so the next worker to restore this
            # checkpoint reads it locally instead of downloading it again.
            if self.cache is not None:
                self.cache.store_content_from_local_file(
                    archive_path,
                    expected_hash=cache_hash,
                    cache_path=plan.origin_key,
                )
            response = self.repository.persist_checkpoint_archive(
                PersistCheckpointArchiveRequest(
                    checkpoint_id=plan.checkpoint_id,
                    origin_key=plan.origin_key,
                    cache_hash=cache_hash,
                    cache_size_bytes=size_bytes,
                    cache_namespace=self.cache_namespace,
                    locality=plan.metadata.locality if plan.metadata is not None else "",
                    accelerator=(plan.metadata.accelerator if plan.metadata is not None else ""),
                )
            )
            return WorkerCheckpointPersistenceResult(
                checkpoint_id=response.checkpoint_id or plan.checkpoint_id,
                archive_path=str(archive_path),
                origin_key=response.origin_key or plan.origin_key,
                cache_hash=response.cache_hash or cache_hash,
                cache_size_bytes=response.cache_size_bytes or size_bytes,
                locality=response.locality,
                accelerator=response.accelerator,
            )
        finally:
            if plan.cleanup_archive_after_persist:
                archive_path.unlink(missing_ok=True)


@dataclass(slots=True)
class RemoteCheckpointRestoreSource:
    repository: WorkerRepositoryHttpClient
    internal_http: InternalHttpClient
    timeout_seconds: float = 300.0
    download_urls: dict[str, str] = field(default_factory=dict)

    def get_checkpoint(self, checkpoint_id: str, *, workspace_id: str) -> CheckpointRecord:
        response = self.repository.get_checkpoint_restore(
            GetCheckpointRestoreRequest(
                checkpoint_id=checkpoint_id,
                workspace_id=workspace_id,
            )
        )
        if response.checkpoint is None or not response.download_url:
            msg = f"checkpoint {checkpoint_id!r} restore metadata was not returned"
            raise RuntimeError(msg)
        self.download_urls[checkpoint_id] = response.download_url
        return response.checkpoint

    def download_checkpoint(self, checkpoint: CheckpointRecord, target: Path) -> None:
        url = self.download_urls.pop(checkpoint.checkpoint_id, "")
        if not url:
            msg = f"checkpoint {checkpoint.checkpoint_id!r} download URL is unavailable"
            raise RuntimeError(msg)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            _download_presigned_url(
                self.internal_http,
                url,
                temporary,
                timeout_seconds=self.timeout_seconds,
                resource_name="checkpoint archive",
            )
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def _put_presigned_checkpoint_archive(
    internal_http: InternalHttpClient,
    url: str,
    path: Path,
    *,
    content_length: int,
) -> None:
    scheme = urlparse(url).scheme
    if scheme not in {"http", "https"}:
        msg = f"unsupported checkpoint upload URL scheme: {scheme}"
        raise ValueError(msg)
    try:
        with path.open("rb") as source:
            response = internal_http.request(
                "PUT",
                url,
                headers={
                    "content-type": "application/x-tar",
                    "content-length": str(content_length),
                },
                content=source,
                timeout_seconds=300,
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise _PresignedTransferError(
                f"checkpoint archive upload returned HTTP {response.status_code}"
            )
    except _PresignedTransferError:
        raise
    except Exception as exc:
        raise _PresignedTransferError(
            f"checkpoint archive upload failed: {type(exc).__name__}"
        ) from None


def _download_presigned_url(
    internal_http: InternalHttpClient,
    url: str,
    target: Path,
    *,
    timeout_seconds: float,
    resource_name: str,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        msg = f"unsupported {resource_name} URL scheme: {parsed.scheme}"
        raise ValueError(msg)
    if parsed.hostname is None:
        msg = f"{resource_name} URL hostname is required"
        raise ValueError(msg)
    try:
        with internal_http.stream("GET", url, timeout_seconds=timeout_seconds) as response:
            if response.status_code < 200 or response.status_code >= 300:
                raise _PresignedTransferError(
                    f"{resource_name} download returned HTTP {response.status_code}"
                )
            with target.open("wb") as output:
                for chunk in response.iter_bytes(PRESIGNED_DOWNLOAD_CHUNK_SIZE_BYTES):
                    output.write(chunk)
    except _PresignedTransferError:
        raise
    except Exception as exc:
        raise _PresignedTransferError(
            f"{resource_name} download failed: {type(exc).__name__}"
        ) from None


class _PresignedTransferError(RuntimeError):
    """A transfer failure named without the signed URL that caused it.

    The presigned URL carries the capability, so neither its text nor the
    original exception may reach a log, an error body, or a durable record.
    """
