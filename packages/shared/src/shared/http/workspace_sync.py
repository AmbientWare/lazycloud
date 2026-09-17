from __future__ import annotations

import struct
from collections.abc import AsyncIterable, Iterable, Iterator
from pathlib import PurePosixPath

from anyio import from_thread, to_thread
from pydantic import Field, field_validator, model_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.http.base import HttpModel

WORKSPACE_SYNC_CONTENT_TYPE = "application/vnd.lazycloud.workspace-sync"
SYNC_CHUNK_BYTES = 1024 * 1024
MAX_SYNC_BATCH_BYTES = 8 * 1024 * 1024
SYNC_TIMEOUT_SECONDS = 300.0
MAX_SYNC_CHANGES = 512
MAX_SYNC_MANIFEST_BYTES = 1024 * 1024


class WorkspaceSyncOperation(StringEnum):
    Delete = "delete"
    Write = "write"
    Abort = "abort"


class WorkspaceSyncEntry(HttpModel):
    operation: WorkspaceSyncOperation
    path: str = Field(min_length=1, max_length=4096)
    mode: int = Field(default=0o644, ge=0, le=0o777)
    size: int = Field(default=0, ge=0, le=MAX_SYNC_BATCH_BYTES)
    offset: int = Field(default=0, ge=0)
    file_size: int = Field(default=0, ge=0)
    transfer_id: str = Field(default="", pattern=r"^(?:[0-9a-f]{32})?$")
    sha256: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if (
            PurePosixPath(value).is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or "\\" in value
            or "\x00" in value
        ):
            raise ValueError("workspace sync requires a relative file path")
        return value

    @model_validator(mode="after")
    def file_identity(self) -> WorkspaceSyncEntry:
        if self.operation is WorkspaceSyncOperation.Write:
            if not self.sha256 or not self.transfer_id:
                raise ValueError("workspace writes require a checksum and transfer identity")
            if self.offset + self.size > self.file_size:
                raise ValueError("workspace chunk exceeds its file size")
        elif self.size or self.sha256 or self.offset or self.file_size:
            raise ValueError("workspace removals cannot contain file data")
        if self.operation is WorkspaceSyncOperation.Abort and not self.transfer_id:
            raise ValueError("workspace abort requires a transfer identity")
        return self


class WorkspaceSyncManifest(HttpModel):
    container_id: str = Field(min_length=1, max_length=128)
    entries: list[WorkspaceSyncEntry] = Field(min_length=1, max_length=MAX_SYNC_CHANGES)

    @model_validator(mode="after")
    def bounded_batch(self) -> WorkspaceSyncManifest:
        if sum(entry.size for entry in self.entries) > MAX_SYNC_BATCH_BYTES:
            raise ValueError("workspace sync batch exceeds its transfer limit")
        return self


class WorkspaceSyncBatch(ContractModel):
    manifest: WorkspaceSyncManifest
    data: Iterable[bytes] = Field(repr=False, exclude=True)

    def _header(self) -> bytes:
        manifest = self.manifest.model_dump_json().encode()
        if len(manifest) > MAX_SYNC_MANIFEST_BYTES:
            raise ValueError("workspace sync manifest is too large")
        return struct.pack("!I", len(manifest)) + manifest

    @property
    def encoded_size(self) -> int:
        return len(self._header()) + sum(entry.size for entry in self.manifest.entries)

    def encode(self) -> Iterator[bytes]:
        yield self._header()
        remaining = sum(entry.size for entry in self.manifest.entries)
        for data in self.data:
            if len(data) > remaining:
                raise ValueError("workspace sync content exceeds its manifest")
            remaining -= len(data)
            for offset in range(0, len(data), SYNC_CHUNK_BYTES):
                yield data[offset : offset + SYNC_CHUNK_BYTES]
        if remaining:
            raise ValueError("workspace sync content is incomplete")

    @classmethod
    def decode(cls, chunks: Iterable[bytes]) -> WorkspaceSyncBatch:
        reader = WorkspaceSyncReader(chunks)
        size = struct.unpack("!I", reader.read(4))[0]
        if size > MAX_SYNC_MANIFEST_BYTES:
            raise ValueError("workspace sync manifest is too large")
        manifest = WorkspaceSyncManifest.model_validate_json(reader.read(size))
        return cls(manifest=manifest, data=reader.remaining())


class WorkspaceSyncReader:
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self.chunks = iter(chunks)
        self.pending = memoryview(b"")

    def read(self, size: int) -> bytes:
        if not 0 <= size <= MAX_SYNC_MANIFEST_BYTES:
            raise ValueError("workspace stream reads must be bounded")
        result = bytearray()
        while len(result) < size:
            if not self.pending:
                try:
                    self.pending = memoryview(next(self.chunks))
                except StopIteration as exc:
                    raise ValueError("workspace sync content is incomplete") from exc
                continue
            take = min(size - len(result), len(self.pending))
            result.extend(self.pending[:take])
            self.pending = self.pending[take:]
        return bytes(result)

    def remaining(self) -> Iterator[bytes]:
        if self.pending:
            yield bytes(self.pending)
            self.pending = memoryview(b"")
        yield from self.chunks

    def finish(self) -> None:
        if self.pending or any(self.chunks):
            raise ValueError("workspace sync content exceeds its manifest")


class WorkspaceSyncResponse(HttpModel):
    applied: int = Field(ge=0)


async def read_workspace_sync_batch(chunks: AsyncIterable[bytes]) -> WorkspaceSyncBatch:
    stream = aiter(chunks)

    async def next_chunk() -> bytes | None:
        return await anext(stream, None)

    def body() -> Iterator[bytes]:
        while (chunk := from_thread.run(next_chunk)) is not None:
            yield chunk

    return await to_thread.run_sync(WorkspaceSyncBatch.decode, body())
