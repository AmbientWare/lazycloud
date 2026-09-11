from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from shared.releases import AgentArtifact

SUPERVISOR = "agent-supervisor.sh"

# The supervisor survives exec and can restore a binary that fails before its
# first successful control exchange. It never changes the machine's credentials.
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
trap 'kill -INT "$child" 2>/dev/null; wait "$child"; exit 1' INT TERM
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


@lru_cache(maxsize=1)
def running_binary_sha256(binary: Path) -> str:
    if not binary.is_file():
        return ""
    with binary.open("rb") as handle:
        if handle.read(4) != b"\x7fELF":
            return ""
        handle.seek(0)
        return hashlib.file_digest(handle, "sha256").hexdigest()


@dataclass(slots=True)
class AgentUpdater:
    binary: Path
    state_dir: Path

    @classmethod
    def running(cls, state_dir: Path) -> AgentUpdater:
        binary = Path(shutil.which(sys.argv[0]) or sys.argv[0]).resolve()
        if not binary.is_file():
            raise RuntimeError("the running agent executable could not be resolved")
        return cls(binary, state_dir)

    def binary_sha256(self) -> str:
        return running_binary_sha256(self.binary)

    def confirm(self) -> None:
        pending = self.state_dir / "agent-update.pending"
        if pending.exists() and pending.read_text().strip() == self.binary_sha256():
            pending.unlink()

    def install(self, artifact: AgentArtifact) -> None:
        if not (self.state_dir / SUPERVISOR).is_file():
            raise RuntimeError(
                "agent automatic updates require reinstalling its supervised service"
            )
        rejected = self.state_dir / "agent-update.rejected"
        if rejected.exists() and rejected.read_text().strip() == artifact.sha256:
            raise RuntimeError("agent release failed startup and was rolled back")
        staged = self.binary.with_name(f".{self.binary.name}.download")
        previous = self.binary.with_name(f"{self.binary.name}.previous")
        digest = hashlib.sha256()
        size = 0
        try:
            with (
                urllib.request.urlopen(artifact.url, timeout=60) as response,
                staged.open("wb") as handle,
            ):
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > artifact.size_bytes:
                        raise RuntimeError("agent update exceeded its declared size")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
                raise RuntimeError("agent update does not match the published artifact")
            staged.chmod(0o755)
            subprocess.run([str(staged), "--help"], check=True, capture_output=True, timeout=30)
            shutil.copy2(self.binary, previous)
            with previous.open("rb") as handle:
                os.fsync(handle.fileno())
            pending = self.state_dir / "agent-update.pending"
            with pending.open("w") as handle:
                handle.write(artifact.sha256 + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staged, self.binary)
            os.execv(str(self.binary), [str(self.binary), *sys.argv[1:]])
        finally:
            staged.unlink(missing_ok=True)


__all__ = ["SUPERVISOR", "SUPERVISOR_SCRIPT", "AgentUpdater"]
