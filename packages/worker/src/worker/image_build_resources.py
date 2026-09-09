from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from uuid import UUID

from worker.funding import WorkerFundingSession
from worker.runtime_config import (
    prepare_container_accounting_cgroup,
    release_container_accounting_cgroup,
)


@dataclass(slots=True)
class ImageBuildResources:
    root: Path
    cancellation: threading.Event = field(default_factory=threading.Event)
    funding: WorkerFundingSession | None = field(default=None, repr=False)
    stopped_monotonic: float | None = field(default=None, init=False)
    _processes: dict[int, subprocess.Popen[bytes]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def create(
        cls, container_id: str, *, cpu_millicores: int, memory_mib: int
    ) -> ImageBuildResources:
        if str(UUID(container_id)) != container_id:
            raise ValueError("image build container ID must be canonical")
        if cpu_millicores <= 0 or memory_mib <= 0:
            raise RuntimeError("image build requires delegated CPU and memory cgroups")
        root = prepare_container_accounting_cgroup(container_id)
        resources = cls(root)
        try:
            (root / "cpu.max").write_text(f"{cpu_millicores * 100} 100000")
            (root / "memory.max").write_text(str(memory_mib * 1024**2))
            (root / "memory.swap.max").write_text("0")
            (root / "memory.oom.group").write_text("1")
            (root / "commands").mkdir()
        except BaseException:
            resources.close()
            raise
        return resources

    def require_valid(self) -> None:
        if self.cancellation.is_set():
            raise RuntimeError("image build was cancelled")
        if self.funding is None:
            raise RuntimeError("image build funding is not authorized")
        self.funding.require_valid()

    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        output_fd: int,
        input_file: Path | None = None,
    ) -> subprocess.Popen[bytes]:
        # A trusted shell joins before exec. Moving a running child afterwards
        # leaves a race where it can fork outside the build's resource boundary.
        wrapper = [
            "/bin/sh",
            "-ec",
            'echo $$ > "$1/cgroup.procs"; shift; exec "$@"',
            "image-build",
            str(self.root / "commands"),
            *command,
        ]
        with self._lock:
            self.require_valid()
            with (
                input_file.open("rb")
                if input_file is not None
                else open(os.devnull, "rb") as stream
            ):
                process = subprocess.Popen(
                    wrapper,
                    cwd=cwd,
                    env=env,
                    stdin=stream,
                    stdout=output_fd,
                    stderr=output_fd,
                    close_fds=True,
                    start_new_session=True,
                )
            self._processes[process.pid] = process
            return process

    def reap(self, process: subprocess.Popen[bytes]) -> None:
        process.wait()
        with self._lock:
            self._processes.pop(process.pid, None)

    def stop(self) -> None:
        with self._lock:
            self.cancellation.set()
            # Also kill wrappers that have exec'd the shell but not joined yet.
            for process in self._processes.values():
                if process.poll() is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
            (self.root / "cgroup.kill").write_text("1")
            if self.stopped_monotonic is None:
                self.stopped_monotonic = monotonic()

    def counters(self) -> tuple[int, int]:
        cpu = dict(line.split() for line in (self.root / "cpu.stat").read_text().splitlines())
        memory = dict(line.split() for line in (self.root / "memory.stat").read_text().splitlines())
        return int(cpu["usage_usec"]), int(memory["anon"]) + int(memory["file_mapped"])

    def quiesce(self) -> None:
        self.stop()
        for process in tuple(self._processes.values()):
            self.reap(process)

    def close(self) -> None:
        self.quiesce()
        release_container_accounting_cgroup(self.root.name)
