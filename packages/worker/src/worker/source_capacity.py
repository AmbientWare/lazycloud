from __future__ import annotations

import errno
import os
from pathlib import Path

SOURCE_MINIMUM_FREE_BYTES = 5 * 1024**3


class SourceCapacityError(OSError):
    def __init__(self) -> None:
        super().__init__(errno.ENOSPC, "source transfer exceeds available worker disk space")


def require_source_space(directory: Path | int, size: int) -> None:
    stat = os.statvfs(directory)
    available = stat.f_bavail * stat.f_frsize
    if size + SOURCE_MINIMUM_FREE_BYTES > available:
        raise SourceCapacityError()
