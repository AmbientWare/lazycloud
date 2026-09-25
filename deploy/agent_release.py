"""What an agent release archive is, for the build that makes one and the release that ships it.

Standard library only: the agent build runs without the workspace installed.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

AGENT_EXECUTABLE = "lazycloud-agent"


def is_agent_release(path: Path) -> bool:
    """Whether `path` is a gzip tarball with the agent executable at its root."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            executable = archive.getmember(f"./{AGENT_EXECUTABLE}")
    except (tarfile.TarError, KeyError, OSError):
        return False
    return executable.isfile() and bool(executable.mode & 0o111)


__all__ = ["AGENT_EXECUTABLE", "is_agent_release"]
