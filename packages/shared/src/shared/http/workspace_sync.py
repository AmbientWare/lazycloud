from __future__ import annotations

import struct
from collections.abc import AsyncIterable
from pathlib import PurePosixPath

from pydantic import Field, field_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.http.base import HttpModel

WORKSPACE_SYNC_CONTENT_TYPE = "application/vnd.lazycloud.workspace-sync"
MAX_SYNC_BYTES = 64 * 1024 * 1024
MAX_SYNC_CHANGES = 512
MAX_SYNC_MANIFEST_BYTES = 1024 * 1024


class WorkspaceSyncOperation(StringEnum):
    Delete = "delete"
    Write = "write"


class WorkspaceSyncEntry(HttpModel):
    operation: WorkspaceSyncOperation
    path: str = Field(min_length=1, max_length=4096)
    mode: int = Field(default=0o644, ge=0, le=0o777)
    size: int = Field(default=0, ge=0, le=MAX_SYNC_BYTES)

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


class WorkspaceSyncManifest(HttpModel):
    container_id: str = Field(min_length=1, max_length=128)
    entries: list[WorkspaceSyncEntry] = Field(min_length=1, max_length=MAX_SYNC_CHANGES)


class WorkspaceSyncBatch(ContractModel):
    manifest: WorkspaceSyncManifest
    data: bytes = Field(default=b"", repr=False)

    def encode(self) -> bytes:
        self.validate_sizes()
        manifest = self.manifest.model_dump_json().encode()
        if len(manifest) > MAX_SYNC_MANIFEST_BYTES:
            raise ValueError("workspace sync manifest is too large")
        return struct.pack("!I", len(manifest)) + manifest + self.data

    def validate_sizes(self) -> None:
        if len(self.data) > MAX_SYNC_BYTES:
            raise ValueError("workspace sync batch exceeds 64 MiB")
        if sum(entry.size for entry in self.manifest.entries) != len(self.data):
            raise ValueError("workspace sync content length does not match its manifest")
        if any(
            entry.size
            for entry in self.manifest.entries
            if entry.operation is WorkspaceSyncOperation.Delete
        ):
            raise ValueError("workspace deletes cannot contain file data")

    @classmethod
    def decode(cls, data: bytes) -> WorkspaceSyncBatch:
        if len(data) < 4 or len(data) > MAX_SYNC_BYTES + MAX_SYNC_MANIFEST_BYTES + 4:
            raise ValueError("invalid workspace sync batch size")
        size = struct.unpack("!I", data[:4])[0]
        if size > MAX_SYNC_MANIFEST_BYTES or size > len(data) - 4:
            raise ValueError("invalid workspace sync manifest size")
        batch = cls(
            manifest=WorkspaceSyncManifest.model_validate_json(data[4 : 4 + size]),
            data=data[4 + size :],
        )
        batch.validate_sizes()
        return batch


class WorkspaceSyncResponse(HttpModel):
    applied: int = Field(ge=0)


async def read_workspace_sync_batch(chunks: AsyncIterable[bytes]) -> WorkspaceSyncBatch:
    data = bytearray()
    async for chunk in chunks:
        if len(data) + len(chunk) > MAX_SYNC_BYTES + MAX_SYNC_MANIFEST_BYTES + 4:
            raise ValueError("workspace sync batch exceeds 64 MiB")
        data.extend(chunk)
    return WorkspaceSyncBatch.decode(bytes(data))
