from __future__ import annotations

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


RELEASE_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STALE_STAGING_SECONDS = 3600
"""How long a download or unpack goes untouched before pruning takes it as abandoned."""
UPDATE_PENDING_FILE = "agent-update.pending"
RELEASE_COMPLETE_FILE = ".lazycloud-release-complete"
"""Written into a release last, when unpacking it is finished; the install script too."""


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
    """

    command: Path
    state_dir: Path

    @classmethod
    def running(cls, state_dir: Path) -> AgentUpdater:
        command = Path(shutil.which(sys.argv[0]) or sys.argv[0]).absolute()
        if not command.exists():
            raise RuntimeError("the running agent executable could not be resolved")
        return cls(command, state_dir)

    @property
    def release(self) -> Path:
        return self.command.resolve().parent

    @property
    def previous(self) -> Path:
        return self.command.with_name(f"{self.command.name}.previous")

    def binary_sha256(self) -> str:
        """The digest of the release archive this agent runs from; empty from a source tree."""
        name = self.release.name
        return name if RELEASE_DIGEST_PATTERN.fullmatch(name) else ""

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
        does staging untouched for STALE_STAGING_SECONDS, which leaves an
        install running now alone. Each removal is best effort: one that
        fails is logged and left for the next start. An agent run from a source
        tree has no releases and keeps everything.
        """
        if not self.binary_sha256():
            return
        keep = {self.release}
        if self.previous.is_symlink():
            keep.add(self.previous.resolve().parent)
        stale_before = time.time() - STALE_STAGING_SECONDS
        for entry in self.release.parent.iterdir():
            if entry in keep:
                continue
            try:
                if entry.name.startswith("."):
                    removing = entry.name.endswith(".removing")
                    if not removing and entry.lstat().st_mtime > stale_before:
                        continue
                elif not RELEASE_DIGEST_PATTERN.fullmatch(entry.name):
                    continue
                discard_release(entry)
            except OSError:
                LOGGER.warning("could not remove the old agent release %s", entry, exc_info=True)

    def install(self, artifact: AgentArtifact, *, before_exec: Callable[[], None]) -> None:
        if not (self.state_dir / SUPERVISOR).is_file():
            raise RuntimeError(
                "agent automatic updates require reinstalling its supervised service"
            )
        if not self.binary_sha256():
            raise RuntimeError("agent automatic updates require an installed agent release")
        rejected = self.state_dir / "agent-update.rejected"
        if rejected.exists() and rejected.read_text().strip() == artifact.sha256:
            raise RuntimeError("agent release failed startup and was rolled back")
        release = self.release.parent / artifact.sha256
        if not release_complete(release):
            if release.exists():
                discard_release(release)
            self._unpack(artifact, release)
        executable = release / self.command.resolve().name
        _replace_link(self.previous, self.command.resolve())
        pending = self.state_dir / UPDATE_PENDING_FILE
        with pending.open("w") as handle:
            handle.write(artifact.sha256 + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_link(self.command, executable)
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
            executable = staged / self.command.resolve().name
            subprocess.run([str(executable), "--help"], check=True, capture_output=True, timeout=30)
            (staged / RELEASE_COMPLETE_FILE).touch()
            _fsync_tree(staged)
            os.replace(staged, release)
            _fsync_directory(release.parent)
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(staged, ignore_errors=True)


def _fsync_tree(root: Path) -> None:
    """Flush every file and directory of a staged release, so a crash never exposes a torn one."""
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
        _fsync_directory(Path(directory))


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_link(link: Path, target: Path) -> None:
    temporary = link.with_name(f".{link.name}.{os.getpid()}.link")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


__all__ = ["SUPERVISOR", "SUPERVISOR_SCRIPT", "AgentUpdateRestartError", "AgentUpdater"]
