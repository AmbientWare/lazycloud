from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from uuid import uuid4

from shared.http.workspace_sync import WorkspaceSyncBatch, WorkspaceSyncOperation


def apply_workspace_batch(root: str, batch: WorkspaceSyncBatch) -> int:
    batch.validate_sizes()
    offset = 0
    for entry in batch.manifest.entries:
        with _parent_directory(
            Path(root), entry.path, create=entry.operation is WorkspaceSyncOperation.Write
        ) as parent:
            name = entry.path.rsplit("/", 1)[-1]
            if parent is None:
                continue
            if entry.operation is WorkspaceSyncOperation.Delete:
                with suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=parent)
            else:
                temporary = f".lazycloud-sync-{uuid4().hex}"
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    entry.mode,
                    dir_fd=parent,
                )
                try:
                    with os.fdopen(descriptor, "wb") as destination:
                        destination.write(memoryview(batch.data)[offset : offset + entry.size])
                        os.fchmod(destination.fileno(), entry.mode)
                    with suppress(FileNotFoundError, NotADirectoryError):
                        os.rmdir(name, dir_fd=parent)
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                finally:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=parent)
        offset += entry.size
    return len(batch.manifest.entries)


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
            except FileNotFoundError:
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
