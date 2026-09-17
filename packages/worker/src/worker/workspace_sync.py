from __future__ import annotations

import errno
import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO

from shared.http.workspace_sync import (
    SYNC_CHUNK_BYTES,
    WorkspaceSyncBatch,
    WorkspaceSyncEntry,
    WorkspaceSyncOperation,
    WorkspaceSyncReader,
)

from worker.source_capacity import require_source_space


def apply_workspace_batch(root: str, batch: WorkspaceSyncBatch) -> int:
    reader = WorkspaceSyncReader(batch.data)
    staging = Path(root).parent / ".source-sync"
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    for entry in batch.manifest.entries:
        temporary = staging / entry.transfer_id
        if entry.operation is WorkspaceSyncOperation.Abort:
            temporary.unlink(missing_ok=True)
            continue
        with _parent_directory(
            Path(root), entry.path, create=entry.operation is WorkspaceSyncOperation.Write
        ) as parent:
            name = entry.path.rsplit("/", 1)[-1]
            if parent is None:
                continue
            if entry.operation is WorkspaceSyncOperation.Delete:
                with suppress(FileNotFoundError, IsADirectoryError):
                    os.unlink(name, dir_fd=parent)
            else:
                _write_chunk(temporary, parent, name, entry, reader)
    reader.finish()
    return len(batch.manifest.entries)


def _write_chunk(
    temporary: Path,
    parent: int,
    name: str,
    entry: WorkspaceSyncEntry,
    reader: WorkspaceSyncReader,
) -> None:
    flags = os.O_RDWR | os.O_NOFOLLOW
    if entry.offset == 0:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except FileNotFoundError:
        # Completion may have reached disk before the acknowledgement reached the client.
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as existing:
            _accept_completed_chunk(existing, entry, reader)
        return
    with os.fdopen(descriptor, "r+b") as destination:
        fcntl.flock(destination.fileno(), fcntl.LOCK_EX)
        if not _owns_temporary(temporary, destination):
            _accept_completed_chunk(destination, entry, reader)
            return
        current_size = os.fstat(destination.fileno()).st_size
        if current_size < entry.offset:
            raise ValueError("workspace chunks must arrive in order")
        require_source_space(parent, max(0, entry.file_size - current_size))
        destination.seek(entry.offset)
        remaining = entry.size
        while remaining:
            chunk = reader.read(min(remaining, SYNC_CHUNK_BYTES))
            require_source_space(parent, len(chunk))
            destination.write(chunk)
            remaining -= len(chunk)
        destination.flush()
        if entry.offset + entry.size != entry.file_size:
            return
        destination.truncate(entry.file_size)
        destination.seek(0)
        if _file_digest(destination) != entry.sha256:
            temporary.unlink(missing_ok=True)
            raise ValueError("workspace file checksum mismatch")
        os.fchmod(destination.fileno(), entry.mode)
        with suppress(FileNotFoundError, NotADirectoryError):
            os.rmdir(name, dir_fd=parent)
        os.replace(temporary, name, dst_dir_fd=parent)


def _owns_temporary(path: Path, source: BinaryIO) -> bool:
    try:
        named = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    opened = os.fstat(source.fileno())
    return (named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino)


def _accept_completed_chunk(
    source: BinaryIO, entry: WorkspaceSyncEntry, reader: WorkspaceSyncReader
) -> None:
    if _file_digest(source) != entry.sha256:
        raise ValueError("workspace transfer is missing; restart the file upload")
    remaining = entry.size
    while remaining:
        chunk = reader.read(min(remaining, SYNC_CHUNK_BYTES))
        remaining -= len(chunk)


def _file_digest(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := source.read(SYNC_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _parent_directory(root: Path, relative: str, *, create: bool) -> Iterator[int | None]:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parents: list[tuple[int, str]] = []
    try:
        for part in relative.split("/")[:-1]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, dir_fd=descriptor)
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            except (FileNotFoundError, NotADirectoryError):
                if create:
                    raise
                yield None
                return
            parents.append((descriptor, part))
            descriptor = child
        yield descriptor
        if not create:
            for parent, name in reversed(parents):
                try:
                    os.rmdir(name, dir_fd=parent)
                except OSError as exc:
                    if exc.errno in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT, errno.ENOTDIR}:
                        break
                    raise
    finally:
        os.close(descriptor)
        for parent, _ in parents:
            os.close(parent)
