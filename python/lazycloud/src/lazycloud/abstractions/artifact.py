from __future__ import annotations

import mimetypes
import shutil
import tempfile
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, NamedTuple, Protocol
from uuid import UUID

from pydantic import JsonValue
from shared.api import (
    CompleteArtifactRequest,
    CreateArtifactRequest,
    ErrorCode,
    PresignArtifactRequest,
)
from shared.app_identity import NAME
from shared.task_context import current_task_id

from lazycloud.clients.api import ApiConnectionError, ApiError
from lazycloud.clients.storage import StorageClient, upload_file_parts
from lazycloud.control import resolve_control_client_config, storage_client

DEFAULT_ARTIFACT_CHUNK_SIZE_BYTES = 1024 * 1024
_REMOTE_STAT_MODE = "0644"


@dataclass(frozen=True)
class ArtifactStat:
    path: Path
    name: str
    size: int
    is_dir: bool
    packaged: bool = False


@dataclass(frozen=True)
class SavedArtifact:
    path: Path
    stat: ArtifactStat
    artifact_id: str = ""
    task_id: str = ""
    filename: str = ""
    remote: bool = False
    expires_at: datetime | None = None


class Stat(NamedTuple):
    mode: str
    size: int
    atime: datetime | None
    mtime: datetime | None

    def to_dict(self) -> dict[str, object]:
        return self._asdict()


class PILImage(Protocol):
    def save(self, fp: str | Path, format: str | None = None, **params: object) -> None: ...


class Artifact:
    _tmp_dir = Path("/tmp/artifacts")

    def __init__(
        self,
        *,
        path: str | Path,
        content_type: str | None = None,
        task_id: str | None = None,
        workspace: str | None = None,
    ) -> None:
        self.prepare_tmp_dir()
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.value = str(self.path)
        self.content_type = content_type
        self._client: StorageClient | None = None
        self.task_id = task_id if task_id is not None else current_task_id()
        self.workspace = workspace
        self.endpoint: str | None = None
        self.token: str | None = None
        self.timeout_seconds = 10.0
        self.id: str | None = None
        self.filename = ""

    def __del__(self) -> None:
        with suppress(FileNotFoundError):
            shutil.rmtree(self._tmp_dir / str(id(self)))

    @classmethod
    def file(
        cls,
        path: str | Path,
        *,
        content_type: str | None = None,
        task_id: str | None = None,
    ) -> Artifact:
        return cls(
            path=path,
            content_type=content_type,
            task_id=task_id,
        )

    @classmethod
    def from_file(
        cls,
        file_handle: BinaryIO,
        *,
        suffix: str = "",
    ) -> Artifact:
        target = Path(tempfile.mkdtemp(prefix=f"{NAME}-artifact-")) / f"artifact{suffix}"
        with target.open("wb") as artifact:
            shutil.copyfileobj(file_handle, artifact)
        return cls.file(target)

    @classmethod
    def from_pil_image(
        cls,
        image: PILImage,
        format: str | None = "png",
    ) -> Artifact:
        cls.prepare_tmp_dir()
        target = Path(tempfile.mkdtemp(prefix=f"{NAME}-artifact-image-")) / "artifact"
        if format:
            suffix = format.lower() if format.startswith(".") else f".{format.lower()}"
            target = target.with_suffix(suffix)
        image.save(target, format=format.lstrip(".") if format else format)
        return cls(path=target)

    @classmethod
    def prepare_tmp_dir(cls) -> None:
        cls._tmp_dir.mkdir(mode=0o755, parents=True, exist_ok=True)

    @property
    def zipped_path(self) -> Path:
        if not self.path.is_dir():
            raise ValueError("Artifact must be a directory to get the zipped path.")
        return self._tmp_dir / str(id(self)) / f"{self.path.name}.zip"

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "value": self.value,
            "path": str(self.path),
            "content_type": self.content_type,
        }
        return result

    def stat(self) -> ArtifactStat | Stat:
        """The saved artifact's size and times once saved remotely, else the local file's."""
        if not self.id:
            return self._local_stat()
        try:
            stored = self._artifact_client().get_artifact(self._remote_id())
        except ApiError as exc:
            if exc.code is ErrorCode.not_found:
                raise ArtifactNotFoundError(exc.message or "artifact not found") from exc
            raise ArtifactReadError(exc.message or "failed to read artifact") from exc
        except ApiConnectionError as exc:
            raise ArtifactReadError(str(exc)) from exc
        stamp = stored.stored_at or stored.created_at
        return Stat(mode=_REMOTE_STAT_MODE, size=stored.size_bytes, atime=stamp, mtime=stamp)

    def delete(self) -> None:
        if not self.id:
            raise ArtifactNotSavedError("artifact has not been saved remotely")
        try:
            self._artifact_client().delete_artifact(self._remote_id())
        except ApiError as exc:
            if exc.code is ErrorCode.not_found:
                raise ArtifactNotFoundError(exc.message or "artifact not found") from exc
            raise ArtifactDeleteError(exc.message or "failed to delete artifact") from exc
        except ApiConnectionError as exc:
            raise ArtifactDeleteError(str(exc)) from exc

    def exists(self) -> bool:
        if not self.id:
            return self.path.exists()
        try:
            self.stat()
        except (ArtifactNotSavedError, ArtifactNotFoundError):
            return False
        return True

    def package(self, *, target_dir: str | Path | None = None) -> Path:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        if self.path.is_file():
            return self.path
        archive_root = target_dir if target_dir is not None else tempfile.mkdtemp()
        return self.zip_dir(self.path, target_dir=archive_root)

    def save(
        self,
        *,
        target_dir: str | Path | None = None,
        task_id: str | None = None,
        chunk_size: int = DEFAULT_ARTIFACT_CHUNK_SIZE_BYTES,
    ) -> SavedArtifact:
        """Save the artifact for a task, or package it locally when there is no task.

        A directory is zipped first. The bytes go straight to the object store.
        """
        effective_client = self._client
        effective_task_id = task_id or self.task_id
        if effective_client is not None or effective_task_id:
            return self._save_remote(
                effective_client or self._artifact_client(),
                task_id=effective_task_id,
                target_dir=target_dir,
                chunk_size=chunk_size,
            )
        return self._save_local(target_dir=target_dir)

    def save_remote(
        self,
        client: StorageClient,
        *,
        task_id: str,
        target_dir: str | Path | None = None,
        chunk_size: int = DEFAULT_ARTIFACT_CHUNK_SIZE_BYTES,
    ) -> SavedArtifact:
        return self._save_remote(
            client,
            task_id=task_id,
            target_dir=target_dir,
            chunk_size=chunk_size,
        )

    def public_url(
        self,
        expires: int = 3600,
        *,
        base_url: str | None = None,
    ) -> str:
        """A URL anyone can download the saved artifact from for `expires` seconds.

        The platform caps the lifetime at the artifact's remaining retention.
        """
        if not self.id:
            if base_url is not None:
                return self._local_public_url(base_url=base_url)
            raise ArtifactNotSavedError("artifact has not been saved remotely")
        request = (
            PresignArtifactRequest(expires_seconds=expires)
            if expires > 0
            else PresignArtifactRequest()
        )
        try:
            response = self._artifact_client().presign_artifact(self._remote_id(), request)
        except ApiError as exc:
            if exc.code is ErrorCode.not_found:
                raise ArtifactNotFoundError(exc.message or "artifact not found") from exc
            raise ArtifactReadError(exc.message or "failed to presign artifact") from exc
        return response.url

    def zip_dir(
        self,
        dir_path: str | Path,
        *,
        target_dir: str | Path | None = None,
        compress_level: int = 9,
    ) -> Path:
        directory = Path(dir_path)
        if not directory.is_dir():
            raise ValueError("Artifact must be a directory to zip.")
        archive = (
            Path(target_dir) / f"{directory.name}.zip"
            if target_dir is not None
            else self.zipped_path
        )
        archive.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        archive.unlink(missing_ok=True)
        with zipfile.ZipFile(
            archive,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=compress_level,
        ) as artifact:
            for path in sorted(item for item in directory.rglob("*") if item.is_file()):
                artifact.write(path, path.relative_to(directory))
        return archive

    def _save_local(self, *, target_dir: str | Path | None = None) -> SavedArtifact:
        packaged = self.package(target_dir=target_dir)
        return SavedArtifact(path=packaged, stat=self._packaged_stat(packaged))

    def _save_remote(
        self,
        client: StorageClient,
        *,
        task_id: str,
        target_dir: str | Path | None = None,
        chunk_size: int = DEFAULT_ARTIFACT_CHUNK_SIZE_BYTES,
    ) -> SavedArtifact:
        if chunk_size <= 0:
            msg = "artifact chunk size must be positive"
            raise ValueError(msg)
        if not task_id:
            raise ArtifactTaskIdError("task_id is required to save an artifact remotely")
        try:
            task = UUID(task_id)
        except ValueError as exc:
            raise ArtifactTaskIdError(f"task_id {task_id!r} is not a task id") from exc
        packaged = self.package(target_dir=target_dir)
        stat = self._packaged_stat(packaged)
        try:
            created = client.create_artifact(
                CreateArtifactRequest(
                    task_id=task,
                    filename=packaged.name,
                    content_type=self.content_type or guess_content_type(packaged),
                    size_bytes=stat.size,
                )
            )
            parts = upload_file_parts(packaged, created.upload.parts)
            stored = client.complete_artifact(
                created.artifact.id,
                CompleteArtifactRequest(parts=parts)
                if created.upload.upload_id is not None
                else CompleteArtifactRequest(),
            )
        except (ApiError, ApiConnectionError) as exc:
            message = exc.message if isinstance(exc, ApiError) else str(exc)
            raise ArtifactSaveError(message or "failed to save artifact") from exc
        saved = SavedArtifact(
            path=packaged,
            stat=stat,
            artifact_id=str(stored.id),
            task_id=task_id,
            filename=stored.filename,
            remote=True,
            expires_at=stored.expires_at,
        )
        self.id = saved.artifact_id
        self.task_id = saved.task_id
        self.filename = saved.filename
        return saved

    def _packaged_stat(self, packaged: Path) -> ArtifactStat:
        return ArtifactStat(
            path=packaged,
            name=packaged.name,
            size=packaged.stat().st_size,
            is_dir=False,
            packaged=self.path.is_dir(),
        )

    def _local_stat(self) -> ArtifactStat:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        if self.path.is_dir():
            size = sum(path.stat().st_size for path in self.path.rglob("*") if path.is_file())
            return ArtifactStat(path=self.path, name=self.path.name, size=size, is_dir=True)
        return ArtifactStat(
            path=self.path,
            name=self.path.name,
            size=self.path.stat().st_size,
            is_dir=False,
        )

    def _local_public_url(self, *, base_url: str) -> str:
        if base_url == "file://":
            return self.path.resolve().as_uri()
        return f"{base_url.rstrip('/')}/{self.path.as_posix().lstrip('/')}"

    def _artifact_client(self) -> StorageClient:
        if self._client is None:
            config = resolve_control_client_config(
                workspace=self.workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            self._client = storage_client(config)
        return self._client

    def _remote_id(self) -> UUID:
        try:
            return UUID(self.id)
        except (TypeError, ValueError) as exc:
            raise ArtifactNotFoundError(f"artifact {self.id!r} not found") from exc


class ArtifactSaveError(RuntimeError):
    pass


class ArtifactReadError(RuntimeError):
    pass


class ArtifactDeleteError(RuntimeError):
    pass


class ArtifactNotSavedError(RuntimeError):
    pass


class ArtifactNotFoundError(RuntimeError):
    pass


class ArtifactCannotRunLocallyError(RuntimeError):
    pass


class ArtifactTaskIdError(RuntimeError):
    pass


def guess_content_type(path: Path) -> str:
    """Name the type from the filename when the caller did not give one.

    Without this every artifact is stored as application/octet-stream, so a
    reader has nothing to decide with and an image or PDF cannot be rendered.
    A zipped directory is always an archive regardless of what it contains.
    """
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


__all__ = [
    "DEFAULT_ARTIFACT_CHUNK_SIZE_BYTES",
    "Artifact",
    "ArtifactCannotRunLocallyError",
    "ArtifactDeleteError",
    "ArtifactNotFoundError",
    "ArtifactNotSavedError",
    "ArtifactReadError",
    "ArtifactSaveError",
    "ArtifactStat",
    "ArtifactTaskIdError",
    "PILImage",
    "SavedArtifact",
    "Stat",
]
