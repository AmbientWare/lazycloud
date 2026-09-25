from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shared.app_identity import AGENT_NAME
from shared.durable_files import fsync_directory, fsync_tree
from shared.releases import AgentArtifact

LOGGER = logging.getLogger(__name__)

SUPERVISOR = "agent-supervisor.sh"

# The supervisor survives exec and points the command back at the previous
# release when a new one fails before its first successful control exchange. It
# never changes the machine's credentials.
SUPERVISOR_SCRIPT = """#!/bin/sh
set -u
state=$1
shift
binary=$1
pending="$state/agent-update.pending"
previous="$binary.previous"
restore() {
    if [ -f "$pending" ] && [ -f "$previous" ]; then
        mv -f "$previous" "$binary" || exit 1
        mv -f "$pending" "$state/agent-update.rejected" || exit 1
        echo 'Agent update failed; restored the previous executable' >&2
    fi
}
restore
"$@" &
child=$!
trap 'kill -TERM "$child" 2>/dev/null; wait "$child"; exit 0' INT TERM
attempts=0
while kill -0 "$child" 2>/dev/null; do
    if [ -f "$pending" ]; then
        attempts=$((attempts + 1))
        echo "Agent update startup poll $attempts; process $child is running" >&2
        if [ "$attempts" -ge 60 ]; then
            kill -KILL "$child" 2>/dev/null
            break
        fi
    else
        attempts=0
    fi
    sleep 5
done
wait "$child"
result=$?
if [ -f "$pending" ]; then
    result=1
fi
restore
exit "$result"
"""


class AgentUpdateRestartError(RuntimeError):
    pass


AGENT_UPDATE_BLOCKED_EXIT_STATUS = 78
"""The agent's exit status when it cannot apply an ordered update; its unit does not restart it."""


class AgentUpdateBlockedError(RuntimeError):
    """The control plane ordered an update this agent cannot apply; it must be joined again."""


RELEASE_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RUNNING_EXECUTABLE = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None
"""The agent executable this process started from, read once; None when run from source.

Every release archive holds its executable as `./lazycloud-agent`, whatever the
command that links to it is named.
"""
STALE_STAGING_SECONDS = 3600
"""How long a download or unpack goes untouched before pruning takes it as abandoned."""
UPDATE_PENDING_FILE = "agent-update.pending"
RELEASE_COMPLETE_FILE = ".lazycloud-release-complete"
"""Written into a release last, when unpacking it is finished; the install script too."""


def installed_command(executable: Path) -> Path | None:
    """The command link the installer points at `executable`, when it is an installed release.

    The installer unpacks each release to `<prefix>/lib/lazycloud-agent/<digest>/`
    and links `<prefix>/bin/lazycloud-agent` to its executable.
    """
    release = executable.parent
    releases = release.parent
    if (
        RELEASE_DIGEST_PATTERN.fullmatch(release.name)
        and releases.name == AGENT_NAME
        and releases.parent.name == "lib"
    ):
        return releases.parent.parent / "bin" / AGENT_NAME
    return None


def discard_abandoned_removals(directory: Path) -> None:
    """Finish removing releases directories an interrupted uninstall renamed in `directory`."""
    for entry in directory.glob(f".{AGENT_NAME}.*.removing"):
        shutil.rmtree(entry)


def release_complete(release: Path) -> bool:
    """Whether `release` was unpacked whole, rather than cut short or half removed."""
    return (release / RELEASE_COMPLETE_FILE).is_file()


def discard_release(entry: Path) -> None:
    """Remove a release or staging entry without leaving part of it under its own name.

    It is renamed to a dot name first, so an interrupted removal leaves staging
    that pruning finishes, never a digest directory that looks installed.
    """
    if entry.is_dir() and not entry.is_symlink():
        doomed = entry.with_name(f".{entry.name.lstrip('.')}.{os.getpid()}.removing")
        os.replace(entry, doomed)
        shutil.rmtree(doomed)
    else:
        entry.unlink(missing_ok=True)


@dataclass(slots=True)
class AgentUpdater:
    """Installs agent releases beside the running one and switches the command to them.

    `command` is the path the service runs, a symlink to `<digest>/lazycloud-agent`
    in the directory of unpacked releases. Each release is unpacked once, into a
    directory named by its archive's verified digest, so that name is the digest
    the agent reports and nothing is hashed or unpacked when it starts.
    `executable` is the one this process runs, which stays the running release
    even after the command is pointed at another.
    """

    command: Path
    state_dir: Path
    executable: Path | None

    @classmethod
    def running(cls, state_dir: Path) -> AgentUpdater:
        command = Path(shutil.which(sys.argv[0]) or sys.argv[0]).absolute()
        return cls(command, state_dir, RUNNING_EXECUTABLE)

    @property
    def release(self) -> Path | None:
        """The directory the running executable was unpacked into."""
        return self.executable.parent if self.executable is not None else None

    @property
    def previous(self) -> Path:
        return self.command.with_name(f"{self.command.name}.previous")

    def binary_sha256(self) -> str:
        """The digest of the artifact this agent runs from; empty from a source tree.

        An installed release is named by its archive's digest. An executable
        placed any other way reports its own digest, so the answer is stable.
        """
        if self.executable is None or self.release is None:
            return ""
        if RELEASE_DIGEST_PATTERN.fullmatch(self.release.name):
            return self.release.name
        return _file_sha256(self.executable)

    def update_blocker(self, artifact: AgentArtifact) -> str:
        """Why this agent cannot apply `artifact` itself, or empty when it can."""
        if self.release is None:
            return "the agent runs from a source tree"
        if not RELEASE_DIGEST_PATTERN.fullmatch(self.release.name):
            return "the agent was not installed as a release"
        if not self.command.is_symlink():
            return f"the agent command {self.command} is not a link to a release"
        if not (self.state_dir / SUPERVISOR).is_file():
            return "the agent service has no update supervisor"
        rejected = self.state_dir / "agent-update.rejected"
        if rejected.exists() and rejected.read_text().strip() == artifact.sha256:
            return "this release failed startup and was rolled back"
        return ""

    def confirm(self) -> None:
        """Accept a new release once it has streamed, and remove all but it and the previous."""
        pending = self.state_dir / UPDATE_PENDING_FILE
        if not pending.exists() or pending.read_text().strip() != self.binary_sha256():
            return
        pending.unlink()
        self.prune()

    def prune(self) -> None:
        """Remove every unpacked release but the running one and the previous.

        Releases are directories named by their digest, and a download or
        unpack in progress, the updater's or the install script's, is named
        with a leading dot. A release rolled back or superseded goes, and so
        does staging, once untouched for STALE_STAGING_SECONDS. That leaves an
        install running now alone, and a release it moved into place but has
        not linked yet. Each removal is best effort: one that
        fails is logged and left for the next start. Only the directory the
        installer lays out, `lib/lazycloud-agent`, holds releases; an agent run
        from anywhere else, or from a source tree, keeps everything.
        """
        running = self.release
        if running is None or self.executable is None or not installed_command(self.executable):
            return
        try:
            discard_abandoned_removals(running.parent.parent)
        except OSError:
            LOGGER.warning("could not remove an abandoned agent releases directory", exc_info=True)
        keep = {running}
        for link in (self.command, self.previous):
            if link.is_symlink():
                keep.add(link.resolve().parent)
        stale_before = time.time() - STALE_STAGING_SECONDS
        for entry in running.parent.iterdir():
            if entry in keep:
                continue
            try:
                staging = entry.name.startswith(".")
                if not staging and not RELEASE_DIGEST_PATTERN.fullmatch(entry.name):
                    continue
                removing = staging and entry.name.endswith(".removing")
                if not removing and entry.lstat().st_mtime > stale_before:
                    continue
                discard_release(entry)
            except OSError:
                LOGGER.warning("could not remove the old agent release %s", entry, exc_info=True)

    def install(self, artifact: AgentArtifact, *, before_exec: Callable[[], None]) -> None:
        """Unpack `artifact` beside the running release and restart into it.

        The caller checks `update_blocker` first; an agent that cannot update
        never gets here.
        """
        running = self.release
        if running is None or self.executable is None or self.update_blocker(artifact):
            raise RuntimeError(self.update_blocker(artifact) or "the agent cannot update")
        release = running.parent / artifact.sha256
        if not release_complete(release):
            if release.exists():
                discard_release(release)
            self._unpack(artifact, release)
        executable = release / AGENT_NAME
        _replace_link(self.previous, self.executable)
        # The supervisor restores from the previous link once it sees the marker.
        fsync_directory(self.previous.parent)
        pending = self.state_dir / UPDATE_PENDING_FILE
        with pending.open("w") as handle:
            handle.write(artifact.sha256 + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(self.state_dir)
        _replace_link(self.command, executable)
        fsync_directory(self.command.parent)
        try:
            before_exec()
            os.execv(str(self.command), [str(self.command), *sys.argv[1:]])
        except Exception as exc:
            raise AgentUpdateRestartError(
                "agent update could not restart after installation"
            ) from exc

    def _unpack(self, artifact: AgentArtifact, release: Path) -> None:
        """Download, verify and unpack a release, then move it into place whole."""
        archive = release.with_name(f".{release.name}.download")
        staged = release.with_name(f".{release.name}.partial")
        shutil.rmtree(staged, ignore_errors=True)
        digest = hashlib.sha256()
        size = 0
        try:
            with (
                urllib.request.urlopen(artifact.url, timeout=60) as response,
                archive.open("wb") as handle,
            ):
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > artifact.size_bytes:
                        raise RuntimeError("agent update exceeded its declared size")
                    digest.update(chunk)
                    handle.write(chunk)
            if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
                raise RuntimeError("agent update does not match the published artifact")
            with tarfile.open(archive, "r:gz") as unpacked:
                unpacked.extractall(staged, filter="data")
            executable = staged / AGENT_NAME
            subprocess.run([str(executable), "--help"], check=True, capture_output=True, timeout=30)
            (staged / RELEASE_COMPLETE_FILE).touch()
            fsync_tree(staged)
            os.replace(staged, release)
            fsync_directory(release.parent)
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(staged, ignore_errors=True)


@functools.cache
def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _replace_link(link: Path, target: Path) -> None:
    temporary = link.with_name(f".{link.name}.{os.getpid()}.link")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


__all__ = [
    "AGENT_UPDATE_BLOCKED_EXIT_STATUS",
    "SUPERVISOR",
    "SUPERVISOR_SCRIPT",
    "AgentUpdateBlockedError",
    "AgentUpdateRestartError",
    "AgentUpdater",
    "discard_abandoned_removals",
    "installed_command",
]
