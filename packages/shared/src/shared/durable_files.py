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


def fsync_tree(root: Path) -> None:
    """Flush every file and directory under `root`, deepest first, skipping symlinks."""
    for directory, _, names in os.walk(root, topdown=False):
        for name in names:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                continue
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        fsync_directory(Path(directory))


__all__ = ["fsync_directory", "fsync_tree"]
