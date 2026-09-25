"""What an agent release archive is, for the build that makes one and the release that ships it.

Every archive holds the agent as `./lazycloud-agent`, the name the install script
and the updater link commands to, however a command itself is named.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

from shared.app_identity import AGENT_NAME


def is_agent_release(path: Path) -> bool:
    """Whether `path` is a gzip tarball with the agent executable at its root."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            executable = archive.getmember(f"./{AGENT_NAME}")
    except (tarfile.TarError, KeyError, OSError):
        return False
    return executable.isfile() and bool(executable.mode & 0o111)


__all__ = ["is_agent_release"]
