from __future__ import annotations

import mimetypes
import shutil
import tempfile
import zipfile
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, NamedTuple, Protocol

from pydantic import JsonValue
from shared.app_identity import NAME
from shared.artifacts import ArtifactRetentionSource, InheritRetention
from shared.http import artifacts
from shared.http.artifacts import (
    ArtifactPublicUrlRequest,
    ArtifactPublicUrlResponse,
    ArtifactSaveResponse,
    ArtifactStatRequest,
    ArtifactStatResponse,
    ArtifactSummary,
)
from shared.http.errors import HttpApiError
from shared.task_context import current_task_id
from typing_extensions import Self

from lazycloud.clients.artifact.control import ArtifactControlClient
from lazycloud.control import ControlClientConfig, resolve_control_client_config

DEFAULT_ARTIFACT_CHUNK_SIZE_BYTES = 1024 * 1024


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
    retention_seconds: int | None = None
    retention_source: ArtifactRetentionSource = ArtifactRetentionSource.Workspace


class ArtifactSaveClient(Protocol):
    def artifact_save_stream(
        self,
        task_id: str,
        filename: str,
        chunks: Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
        retention_seconds: int | InheritRetention | None = InheritRetention.Workspace,
    ) -> ArtifactSaveResponse: ...


class ArtifactMetadataClient(Protocol):
    def delete(self, artifact_id: str) -> None: ...

    def update_retention(
        self, artifact_id: str, retention_seconds: int | None
    ) -> ArtifactSummary: ...

    def artifact_stat(self, request: ArtifactStatRequest) -> ArtifactStatResponse: ...

    def artifact_public_url(
        self, request: ArtifactPublicUrlRequest
    ) -> ArtifactPublicUrlResponse: ...


class ArtifactRemoteClient(ArtifactSaveClient, ArtifactMetadataClient, Protocol):
    pass


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
        retention_seconds: int | InheritRetention | None = InheritRetention.Workspace,
    ) -> None:
        self.prepare_tmp_dir()
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.value = str(self.path)
        self.content_type = content_type
        if (
            not isinstance(retention_seconds, InheritRetention)
            and retention_seconds is not None
            and (
                isinstance(retention_seconds, bool)
                or not isinstance(retention_seconds, int)
                or retention_seconds <= 0
            )
        ):
            raise ValueError("retention_seconds must be a positive integer or None")
        self.retention_seconds = retention_seconds
        self._client: ArtifactRemoteClient | None = None
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
        retention_seconds: int | InheritRetention | None = InheritRetention.Workspace,
    ) -> Artifact:
        return cls(
            path=path,
            content_type=content_type,
            task_id=task_id,
            retention_seconds=retention_seconds,
        )

    def _bind_control(
        self,
        client: ArtifactRemoteClient | None = None,
        *,
        workspace: str | None = None,
        endpoint: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Self:
        self._client = client
        if workspace is not None:
            self.workspace = workspace
        if endpoint is not None:
            self.endpoint = endpoint
        if token is not None:
            self.token = token
        if timeout_seconds is not None:
            self.timeout_seconds = timeout_seconds
        return self

    @classmethod
    def from_file(
        cls,
        file_handle: BinaryIO,
        *,
        suffix: str = "",
        retention_seconds: int | InheritRetention | None = InheritRetention.Workspace,
    ) -> Artifact:
        target = Path(tempfile.mkdtemp(prefix=f"{NAME}-artifact-")) / f"artifact{suffix}"
        with target.open("wb") as artifact:
            shutil.copyfileobj(file_handle, artifact)
        return cls.file(target, retention_seconds=retention_seconds)

    @classmethod
    def from_pil_image(
        cls,
        image: PILImage,
        format: str | None = "png",
        *,
        retention_seconds: int | InheritRetention | None = InheritRetention.Workspace,
    ) -> Artifact:
        cls.prepare_tmp_dir()
        target = Path(tempfile.mkdtemp(prefix=f"{NAME}-artifact-image-")) / "artifact"
        if format:
            suffix = format.lower() if format.startswith(".") else f".{format.lower()}"
            target = target.with_suffix(suffix)
        image.save(target, format=format.lstrip(".") if format else format)
        return cls(path=target, retention_seconds=retention_seconds)

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
        if not isinstance(self.retention_seconds, InheritRetention):
            result["retention_seconds"] = self.retention_seconds
        return result

    def stat(self) -> ArtifactStat | Stat:
        if self.id:
            try:
                response = self._artifact_client().artifact_stat(
                    ArtifactStatRequest(
                        id=self.id,
                        task_id=self._remote_task_id(),
                        filename=self._remote_filename(),
                    )
                )
            except HttpApiError as exc:
                if exc.status_code == 404:
                    raise ArtifactNotFoundError(exc.detail or "artifact not found") from exc
                raise ArtifactReadError(exc.detail or "failed to read artifact") from exc
            if response.stat is None:
                raise ArtifactNotFoundError("artifact not found")
            return _remote_stat(response.stat)
        return self._local_stat()

    def delete(self) -> None:
        if not self.id:
            raise ArtifactNotSavedError("artifact has not been saved remotely")
        try:
            self._artifact_client().delete(self.id)
        except HttpApiError as exc:
            raise ArtifactDeleteError(exc.detail or "failed to delete artifact") from exc

    def set_retention(self, retention_seconds: int | None) -> ArtifactSummary:
        if not self.id:
            raise ArtifactNotSavedError("artifact has not been saved remotely")
        try:
            return self._artifact_client().update_retention(self.id, retention_seconds)
        except HttpApiError as exc:
            raise ArtifactRetentionError(exc.detail or "failed to update retention") from exc

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
        client: ArtifactSaveClient,
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
        if not self.id:
            if base_url is not None:
                return self._local_public_url(base_url=base_url)
            raise ArtifactNotSavedError("artifact has not been saved remotely")
        response = self._artifact_client().artifact_public_url(
            ArtifactPublicUrlRequest(
                id=self.id,
                task_id=self._remote_task_id(),
                filename=self._remote_filename(),
                expires=expires,
            )
        )
        return response.public_url

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
        stat = ArtifactStat(
            path=packaged,
            name=packaged.name,
            size=packaged.stat().st_size,
            is_dir=False,
            packaged=self.path.is_dir(),
        )
        return SavedArtifact(path=packaged, stat=stat)

    def _save_remote(
        self,
        client: ArtifactSaveClient,
        *,
        task_id: str,
        target_dir: str | Path | None = None,
        chunk_size: int = DEFAULT_ARTIFACT_CHUNK_SIZE_BYTES,
    ) -> SavedArtifact:
        if not task_id:
            raise ArtifactTaskIdError("task_id is required to save an artifact remotely")
        packaged = self.package(target_dir=target_dir)
        try:
            response = client.artifact_save_stream(
                task_id,
                packaged.name,
                _file_chunks(packaged, chunk_size=chunk_size),
                content_type=self.content_type or guess_content_type(packaged),
                retention_seconds=self.retention_seconds,
            )
        except HttpApiError as exc:
            raise ArtifactSaveError(exc.detail or "failed to save artifact") from exc
        stat = ArtifactStat(
            path=packaged,
            name=packaged.name,
            size=packaged.stat().st_size,
            is_dir=False,
            packaged=self.path.is_dir(),
        )
        saved = SavedArtifact(
            path=packaged,
            stat=stat,
            artifact_id=response.id,
            task_id=task_id,
            filename=packaged.name,
            remote=True,
            expires_at=response.expires_at,
            retention_seconds=response.retention_seconds,
            retention_source=response.retention_source,
        )
        self.id = saved.artifact_id
        self.task_id = saved.task_id
        self.filename = saved.filename
        return saved

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

    def _artifact_client(self) -> ArtifactRemoteClient:
        if self._client is None:
            config = resolve_control_client_config(
                workspace=self.workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            self._client = _default_artifact_client(config)
        return self._client

    def _remote_task_id(self) -> str:
        if not self.task_id:
            raise ArtifactTaskIdError("task_id is required to read saved artifact metadata")
        return self.task_id

    def _remote_filename(self) -> str:
        return self.filename or (self.zipped_path.name if self.path.is_dir() else self.path.name)


class ArtifactSaveError(RuntimeError):
    pass


class ArtifactReadError(RuntimeError):
    pass


class ArtifactDeleteError(RuntimeError):
    pass


class ArtifactRetentionError(RuntimeError):
    pass


class ArtifactNotSavedError(RuntimeError):
    pass


class ArtifactNotFoundError(RuntimeError):
    pass


class ArtifactCannotRunLocallyError(RuntimeError):
    pass


class ArtifactTaskIdError(RuntimeError):
    pass


def _default_artifact_client(config: ControlClientConfig) -> ArtifactControlClient:
    return ArtifactControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def _remote_stat(stat: artifacts.ArtifactStat) -> Stat:
    return Stat(
        mode=stat.mode,
        size=stat.size or 0,
        atime=stat.atime,
        mtime=stat.mtime,
    )


def guess_content_type(path: Path) -> str:
    """Name the type from the filename when the caller did not give one.

    Without this every artifact is stored as application/octet-stream, so a
    reader has nothing to decide with and an image or PDF cannot be rendered.
    A zipped directory is always an archive regardless of what it contains.
    """
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _file_chunks(path: Path, *, chunk_size: int) -> Iterable[bytes]:
    if chunk_size <= 0:
        msg = "artifact chunk size must be positive"
        raise ValueError(msg)
    if path.stat().st_size == 0:
        yield b""
        return
    with path.open("rb") as file:
        while chunk := file.read(chunk_size):
            yield chunk


__all__ = [
    "DEFAULT_ARTIFACT_CHUNK_SIZE_BYTES",
    "Artifact",
    "ArtifactCannotRunLocallyError",
    "ArtifactDeleteError",
    "ArtifactMetadataClient",
    "ArtifactNotFoundError",
    "ArtifactNotSavedError",
    "ArtifactReadError",
    "ArtifactRemoteClient",
    "ArtifactRetentionError",
    "ArtifactSaveClient",
    "ArtifactSaveError",
    "ArtifactStat",
    "ArtifactTaskIdError",
    "PILImage",
    "SavedArtifact",
    "Stat",
]
