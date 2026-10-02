"""Flushing written files and directories to disk, one file at a time.

Each call flushes only what it names; nothing here syncs a whole filesystem.
"""

from __future__ import annotations

import os
from pathlib import Path


def fsync_directory(directory: Path) -> None:
    """Flush a directory's entries, so a file created or renamed in it survives a crash."""
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["fsync_directory"]
