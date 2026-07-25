from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, NamedTuple, Protocol

from shared.app_identity import NAME
from shared.http import outputs
from shared.http.errors import HttpApiError
from shared.http.outputs import (
    OutputPublicUrlRequest,
    OutputPublicUrlResponse,
    OutputSaveResponse,
    OutputStatRequest,
    OutputStatResponse,
)
from typing_extensions import Self

from lazycloud.clients.output.control import OutputControlClient
from lazycloud.control import ControlClientConfig, resolve_control_client_config

DEFAULT_OUTPUT_CHUNK_SIZE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class OutputStat:
    path: Path
    name: str
    size: int
    is_dir: bool
    packaged: bool = False


@dataclass(frozen=True)
class SavedOutput:
    path: Path
    stat: OutputStat
    output_id: str = ""
    task_id: str = ""
    filename: str = ""
    remote: bool = False

    def remote_stat(self, client: OutputMetadataClient) -> Stat:
        if not self.output_id or not self.task_id:
            raise OutputNotSavedError("output has not been saved remotely")
        try:
            response = client.output_stat(
                OutputStatRequest(
                    id=self.output_id,
                    task_id=self.task_id,
                    filename=self.filename or self.path.name,
                )
            )
        except HttpApiError as exc:
            raise OutputNotFoundError(exc.detail or "output not found") from exc
        if response.stat is None:
            raise OutputNotFoundError("output not found")
        return _remote_stat(response.stat)

    def remote_public_url(
        self,
        client: OutputMetadataClient,
        *,
        expires: int = 3600,
        gateway_external_url: str = "http://127.0.0.1:9000",
    ) -> str:
        if not self.output_id or not self.task_id:
            raise OutputNotSavedError("output has not been saved remotely")
        try:
            response = client.output_public_url(
                OutputPublicUrlRequest(
                    id=self.output_id,
                    task_id=self.task_id,
                    filename=self.filename or self.path.name,
                    expires=expires,
                    gateway_external_url=gateway_external_url,
                )
            )
        except HttpApiError as exc:
            raise OutputPublicURLError(exc.detail or "failed to create output public URL") from exc
        return response.public_url


class OutputSaveClient(Protocol):
    def output_save_stream(
        self,
        task_id: str,
        filename: str,
        chunks: Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> OutputSaveResponse: ...


class OutputMetadataClient(Protocol):
    def output_stat(self, request: OutputStatRequest) -> OutputStatResponse: ...

    def output_public_url(self, request: OutputPublicUrlRequest) -> OutputPublicUrlResponse: ...


class OutputRemoteClient(OutputSaveClient, OutputMetadataClient, Protocol):
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


class Output:
    _tmp_dir = Path("/tmp/outputs")

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
        self.value: Any = str(self.path)
        self.content_type = content_type
        self._client: OutputRemoteClient | None = None
        self.task_id = task_id if task_id is not None else os.getenv("TASK_ID", "")
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
    ) -> Output:
        return cls(path=path, content_type=content_type, task_id=task_id)

    def _bind_control(
        self,
        client: OutputRemoteClient | None = None,
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
    def from_file(cls, file_handle: BinaryIO, *, suffix: str = "") -> Output:
        target = Path(tempfile.mkdtemp(prefix=f"{NAME}-output-")) / f"output{suffix}"
        with target.open("wb") as output:
            shutil.copyfileobj(file_handle, output)
        return cls.file(target)

    @classmethod
    def from_pil_image(cls, image: PILImage, format: str | None = "png") -> Output:
        cls.prepare_tmp_dir()
        target = Path(tempfile.mkdtemp(prefix=f"{NAME}-output-image-")) / "output"
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
            raise ValueError("Output must be a directory to get the zipped path.")
        return self._tmp_dir / str(id(self)) / f"{self.path.name}.zip"

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "path": str(self.path),
            "content_type": self.content_type,
        }

    def stat(self) -> OutputStat | Stat:
        if self.id:
            response = self._output_client().output_stat(
                OutputStatRequest(
                    id=self.id,
                    task_id=self._remote_task_id(),
                    filename=self._remote_filename(),
                )
            )
            if response.stat is None:
                raise OutputNotFoundError("output not found")
            return _remote_stat(response.stat)
        return self._local_stat()

    def exists(self) -> bool:
        if not self.id:
            return self.path.exists()
        try:
            self.stat()
        except (OutputNotSavedError, OutputNotFoundError):
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
        chunk_size: int = DEFAULT_OUTPUT_CHUNK_SIZE_BYTES,
    ) -> SavedOutput:
        effective_client = self._client
        effective_task_id = task_id or self.task_id
        if effective_client is not None or effective_task_id:
            return self._save_remote(
                effective_client or self._output_client(),
                task_id=effective_task_id,
                target_dir=target_dir,
                chunk_size=chunk_size,
            )
        return self._save_local(target_dir=target_dir)

    def save_remote(
        self,
        client: OutputSaveClient,
        *,
        task_id: str,
        target_dir: str | Path | None = None,
        chunk_size: int = DEFAULT_OUTPUT_CHUNK_SIZE_BYTES,
    ) -> SavedOutput:
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
        gateway_external_url: str = "http://127.0.0.1:9000",
        base_url: str | None = None,
    ) -> str:
        if not self.id:
            if base_url is not None:
                return self._local_public_url(base_url=base_url)
            raise OutputNotSavedError("output has not been saved remotely")
        response = self._output_client().output_public_url(
            OutputPublicUrlRequest(
                id=self.id,
                task_id=self._remote_task_id(),
                filename=self._remote_filename(),
                expires=expires,
                gateway_external_url=gateway_external_url,
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
            raise ValueError("Output must be a directory to zip.")
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
        ) as output:
            for path in sorted(item for item in directory.rglob("*") if item.is_file()):
                output.write(path, path.relative_to(directory))
        return archive

    def _save_local(self, *, target_dir: str | Path | None = None) -> SavedOutput:
        packaged = self.package(target_dir=target_dir)
        stat = OutputStat(
            path=packaged,
            name=packaged.name,
            size=packaged.stat().st_size,
            is_dir=False,
            packaged=self.path.is_dir(),
        )
        return SavedOutput(path=packaged, stat=stat)

    def _save_remote(
        self,
        client: OutputSaveClient,
        *,
        task_id: str,
        target_dir: str | Path | None = None,
        chunk_size: int = DEFAULT_OUTPUT_CHUNK_SIZE_BYTES,
    ) -> SavedOutput:
        if not task_id:
            raise OutputTaskIdError("task_id is required to save an output remotely")
        packaged = self.package(target_dir=target_dir)
        try:
            response = client.output_save_stream(
                task_id,
                packaged.name,
                _file_chunks(packaged, chunk_size=chunk_size),
                content_type=self.content_type or "application/octet-stream",
            )
        except HttpApiError as exc:
            raise OutputSaveError(exc.detail or "failed to save output") from exc
        stat = OutputStat(
            path=packaged,
            name=packaged.name,
            size=packaged.stat().st_size,
            is_dir=False,
            packaged=self.path.is_dir(),
        )
        saved = SavedOutput(
            path=packaged,
            stat=stat,
            output_id=response.id,
            task_id=task_id,
            filename=packaged.name,
            remote=True,
        )
        self.id = saved.output_id
        self.task_id = saved.task_id
        self.filename = saved.filename
        return saved

    def _local_stat(self) -> OutputStat:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        if self.path.is_dir():
            size = sum(path.stat().st_size for path in self.path.rglob("*") if path.is_file())
            return OutputStat(path=self.path, name=self.path.name, size=size, is_dir=True)
        return OutputStat(
            path=self.path,
            name=self.path.name,
            size=self.path.stat().st_size,
            is_dir=False,
        )

    def _local_public_url(self, *, base_url: str) -> str:
        if base_url == "file://":
            return self.path.resolve().as_uri()
        return f"{base_url.rstrip('/')}/{self.path.as_posix().lstrip('/')}"

    def _output_client(self) -> OutputRemoteClient:
        if self._client is None:
            config = resolve_control_client_config(
                workspace=self.workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            self._client = _default_output_client(config)
        return self._client

    def _remote_task_id(self) -> str:
        if not self.task_id:
            raise OutputTaskIdError("task_id is required to read saved output metadata")
        return self.task_id

    def _remote_filename(self) -> str:
        return self.filename or (self.zipped_path.name if self.path.is_dir() else self.path.name)


class OutputSaveError(RuntimeError):
    pass


class OutputNotSavedError(RuntimeError):
    pass


class OutputNotFoundError(RuntimeError):
    pass


class OutputPublicURLError(RuntimeError):
    pass


OutputPublicUrlError = OutputPublicURLError


class OutputCannotRunLocallyError(RuntimeError):
    pass


class OutputTaskIdError(RuntimeError):
    pass


def _default_output_client(config: ControlClientConfig) -> OutputControlClient:
    return OutputControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def _remote_stat(stat: outputs.OutputStat) -> Stat:
    return Stat(
        mode=stat.mode,
        size=stat.size or 0,
        atime=stat.atime,
        mtime=stat.mtime,
    )


def _file_chunks(path: Path, *, chunk_size: int) -> Iterable[bytes]:
    if chunk_size <= 0:
        msg = "output chunk size must be positive"
        raise ValueError(msg)
    if path.stat().st_size == 0:
        yield b""
        return
    with path.open("rb") as file:
        while chunk := file.read(chunk_size):
            yield chunk


__all__ = [
    "DEFAULT_OUTPUT_CHUNK_SIZE_BYTES",
    "Output",
    "OutputCannotRunLocallyError",
    "OutputMetadataClient",
    "OutputNotFoundError",
    "OutputNotSavedError",
    "OutputPublicURLError",
    "OutputPublicUrlError",
    "OutputRemoteClient",
    "OutputSaveClient",
    "OutputSaveError",
    "OutputStat",
    "OutputTaskIdError",
    "PILImage",
    "SavedOutput",
    "Stat",
]
