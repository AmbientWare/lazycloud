from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock, Thread

from worker.container_service.models import (
    SandboxDockerDaemonStatus,
    SandboxProcessEvent,
    SandboxProcessEventType,
    WorkerContainerServiceInstance,
)
from worker.container_service.protocols import (
    WorkerContainerInstanceStore,
    WorkerSandboxProcessManager,
    WorkerSandboxProcessManagerFactory,
)
from worker.sandbox_server import SandboxLogStream

LOGGER = logging.getLogger(__name__)

DOCKER_DAEMON_COMMAND = [
    "dockerd",
    "--iptables=false",
    "--ip6tables=false",
    "--bridge=none",
    "--storage-driver=vfs",
    "--userland-proxy=false",
]
DOCKER_SANDBOX_ENV = [
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
]
DOCKER_DAEMON_DIAGNOSTIC_BYTES_PER_STREAM = 4_096
DOCKER_CGROUP_SETUP = """
set -e
mkdir -p /sys/fs/cgroup
if ! grep -q ' /sys/fs/cgroup ' /proc/self/mountinfo; then
  mount -t tmpfs cgroups /sys/fs/cgroup
fi
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
  exit 0
fi
mkdir -p /sys/fs/cgroup/devices
if ! grep -q ' /sys/fs/cgroup/devices ' /proc/self/mountinfo; then
  mount -t cgroup -o devices devices /sys/fs/cgroup/devices
fi
""".strip()
DOCKER_SHUTDOWN = """
set +e
run() {
  if command -v timeout >/dev/null 2>&1; then timeout 3s "$@"; else "$@"; fi
}
if command -v docker >/dev/null 2>&1; then
  ids="$(run docker ps -q 2>/dev/null || true)"
  [ -z "$ids" ] || run docker kill $ids >/dev/null 2>&1 || true
  ids="$(run docker ps -aq 2>/dev/null || true)"
  [ -z "$ids" ] || run docker rm -f $ids >/dev/null 2>&1 || true
fi
if command -v pkill >/dev/null 2>&1; then
  pkill -TERM dockerd >/dev/null 2>&1 || true
  pkill -TERM containerd-shim >/dev/null 2>&1 || true
  pkill -TERM containerd >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5; do
    pgrep dockerd >/dev/null 2>&1 || pgrep containerd >/dev/null 2>&1 || \
      pgrep containerd-shim >/dev/null 2>&1 || exit 0
    sleep 0.2
  done
  pkill -KILL dockerd >/dev/null 2>&1 || true
  pkill -KILL containerd-shim >/dev/null 2>&1 || true
  pkill -KILL containerd >/dev/null 2>&1 || true
fi
exit 0
""".strip()


@dataclass(slots=True)
class _LifecycleLockEntry:
    lock: Lock = field(default_factory=Lock)
    users: int = 0


@dataclass(slots=True)
class _ContainerLifecycleLocks:
    _guard: Lock = field(default_factory=Lock)
    _entries: dict[str, _LifecycleLockEntry] = field(default_factory=dict)

    @contextmanager
    def hold(self, container_id: str) -> Iterator[None]:
        with self._guard:
            entry = self._entries.setdefault(container_id, _LifecycleLockEntry())
            entry.users += 1
        try:
            with entry.lock:
                yield
        finally:
            with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(container_id) is entry:
                    del self._entries[container_id]


@dataclass(slots=True)
class _BoundedDaemonOutput:
    max_bytes_per_stream: int = DOCKER_DAEMON_DIAGNOSTIC_BYTES_PER_STREAM
    _stdout: bytearray = field(default_factory=bytearray)
    _stderr: bytearray = field(default_factory=bytearray)
    _stdout_truncated: bool = False
    _stderr_truncated: bool = False

    def append(self, event: SandboxProcessEvent) -> None:
        if event.stream is SandboxLogStream.Stderr:
            self._stderr_truncated = self._stderr_truncated or self._append(
                self._stderr, event.data
            )
            return
        self._stdout_truncated = self._stdout_truncated or self._append(self._stdout, event.data)

    def render(self) -> str:
        parts: list[str] = []
        for label, data, truncated in (
            ("stderr", self._stderr, self._stderr_truncated),
            ("stdout", self._stdout, self._stdout_truncated),
        ):
            text = bytes(data).decode("utf-8", errors="replace").strip()
            if not text:
                continue
            prefix = "[truncated] " if truncated else ""
            parts.append(f"{label}: {prefix}{text}")
        return "; ".join(parts)

    def _append(self, target: bytearray, data: bytes) -> bool:
        if not data:
            return False
        target.extend(data)
        if len(target) <= self.max_bytes_per_stream:
            return False
        del target[: len(target) - self.max_bytes_per_stream]
        return True


@dataclass(slots=True)
class WorkerSandboxDockerService:
    instances: WorkerContainerInstanceStore
    process_managers: WorkerSandboxProcessManagerFactory
    startup_timeout_seconds: float = 30.0
    ready_poll_seconds: float = 0.25
    _lifecycle_locks: _ContainerLifecycleLocks = field(
        default_factory=_ContainerLifecycleLocks,
        init=False,
        repr=False,
    )

    def prepare(self, container_id: str) -> None:
        instance = self.instances.get_container_instance(container_id)
        if instance is None:
            raise RuntimeError(f"sandbox {container_id} was not recorded before Docker startup")
        self.ensure_ready(instance)

    def ensure_ready(self, instance: WorkerContainerServiceInstance) -> None:
        with self._lifecycle_locks.hold(instance.container_id):
            current = self.instances.get_container_instance(instance.container_id)
            if current is None:
                raise RuntimeError("sandbox disappeared before Docker startup")
            self._ensure_ready_locked(current)

    def _ensure_ready_locked(self, instance: WorkerContainerServiceInstance) -> None:
        if not instance.docker_enabled:
            return
        if instance.docker_daemon_status is SandboxDockerDaemonStatus.Ready:
            if self._probe(instance):
                return
            instance.docker_daemon_status = SandboxDockerDaemonStatus.Failed
            instance.docker_daemon_error = "Docker daemon stopped responding"
            self.instances.save_container_instance(instance)
            raise RuntimeError(instance.docker_daemon_error)
        if instance.docker_daemon_status is SandboxDockerDaemonStatus.Starting:
            self._wait_ready(instance)
            return
        try:
            self._run(instance, ["sh", "-c", DOCKER_CGROUP_SETUP], name="cgroup setup")
            self._run(
                instance,
                ["sh", "-c", "echo 1 > /proc/sys/net/ipv4/ip_forward"],
                name="IPv4 forwarding",
            )
            self._start(instance)
            self._wait_ready(instance)
        except Exception as exc:
            self._stop_locked(instance.container_id)
            instance.docker_daemon_pid = 0
            instance.docker_daemon_status = SandboxDockerDaemonStatus.Failed
            instance.docker_daemon_error = str(exc)
            self.instances.save_container_instance(instance)
            raise

    def stop(self, container_id: str) -> None:
        with self._lifecycle_locks.hold(container_id):
            self._stop_locked(container_id)

    def _stop_locked(self, container_id: str) -> None:
        instance = self.instances.get_container_instance(container_id)
        if instance is None or not instance.docker_enabled:
            return
        instance.docker_daemon_status = SandboxDockerDaemonStatus.Stopping
        instance.docker_daemon_error = ""
        self.instances.save_container_instance(instance)
        if instance.sandbox_process_manager_ready:
            try:
                self._run(
                    instance,
                    ["sh", "-c", DOCKER_SHUTDOWN],
                    name="Docker sandbox shutdown",
                )
            except Exception as exc:
                instance.docker_daemon_error = str(exc)
        instance.docker_daemon_pid = 0
        instance.docker_daemon_status = SandboxDockerDaemonStatus.Stopped
        self.instances.save_container_instance(instance)

    def _start(self, instance: WorkerContainerServiceInstance) -> None:
        manager = self.process_managers.create_process_manager(instance)
        events = iter(manager.stream_exec(DOCKER_DAEMON_COMMAND, cwd="/", env=DOCKER_SANDBOX_ENV))
        try:
            for event in events:
                if event.event_type is SandboxProcessEventType.Chunk:
                    manager.ack(event.pid, event.seq, ok=True)
                    continue
                if event.event_type is SandboxProcessEventType.Exited:
                    raise RuntimeError(f"dockerd exited during startup with code {event.exit_code}")
                instance.docker_daemon_pid = event.pid
                instance.docker_daemon_status = SandboxDockerDaemonStatus.Starting
                instance.docker_daemon_error = ""
                self.instances.save_container_instance(instance)
                Thread(
                    target=self._consume_daemon,
                    args=(instance.container_id, manager, events),
                    name=f"sandbox-dockerd-{instance.container_id}",
                    daemon=True,
                ).start()
                return
        except Exception:
            manager.cleanup()
            raise
        manager.cleanup()
        raise RuntimeError("dockerd stream closed before process start")

    def _wait_ready(self, instance: WorkerContainerServiceInstance) -> None:
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline:
            current = self.instances.get_container_instance(instance.container_id)
            if current is None:
                raise RuntimeError("sandbox disappeared while Docker was starting")
            if current.docker_daemon_status is SandboxDockerDaemonStatus.Failed:
                raise RuntimeError(current.docker_daemon_error or "dockerd exited during startup")
            if self._probe(current):
                current.docker_daemon_status = SandboxDockerDaemonStatus.Ready
                current.docker_daemon_error = ""
                self.instances.save_container_instance(current)
                return
            time.sleep(self.ready_poll_seconds)
        raise RuntimeError("Docker daemon did not become ready before the startup timeout")

    def _probe(self, instance: WorkerContainerServiceInstance) -> bool:
        try:
            self._run(instance, ["docker", "info"], name="docker info")
        except Exception:
            LOGGER.debug("docker readiness probe failed", exc_info=True)
            return False
        return True

    def _run(
        self,
        instance: WorkerContainerServiceInstance,
        argv: list[str],
        *,
        name: str,
    ) -> str:
        manager = self.process_managers.create_process_manager(instance)
        output: list[str] = []
        try:
            for event in manager.stream_exec(argv, cwd="/", env=DOCKER_SANDBOX_ENV):
                if event.event_type is SandboxProcessEventType.Chunk:
                    output.append(event.data.decode("utf-8", errors="replace"))
                    manager.ack(event.pid, event.seq, ok=True)
                elif event.event_type is SandboxProcessEventType.Exited:
                    if event.exit_code != 0:
                        detail = "".join(output).strip()
                        raise RuntimeError(
                            f"{name} failed with exit code {event.exit_code}"
                            + (f": {detail}" if detail else "")
                        )
                    return "".join(output)
        finally:
            manager.cleanup()
        raise RuntimeError(f"{name} stream closed before process exit")

    def _consume_daemon(
        self,
        container_id: str,
        manager: WorkerSandboxProcessManager,
        events: Iterable[SandboxProcessEvent],
    ) -> None:
        exit_code: int | None = None
        stream_error = ""
        output = _BoundedDaemonOutput()
        try:
            for event in events:
                if event.event_type is SandboxProcessEventType.Chunk:
                    output.append(event)
                    manager.ack(event.pid, event.seq, ok=True)
                elif event.event_type is SandboxProcessEventType.Exited:
                    exit_code = event.exit_code
                    break
        except Exception as exc:
            stream_error = f"{type(exc).__name__}: {exc}"
        finally:
            manager.cleanup()
        instance = self.instances.get_container_instance(container_id)
        if instance is None:
            return
        if instance.docker_daemon_status in {
            SandboxDockerDaemonStatus.Stopping,
            SandboxDockerDaemonStatus.Stopped,
        }:
            return
        instance.docker_daemon_status = SandboxDockerDaemonStatus.Failed
        error = stream_error or (
            "dockerd stream closed unexpectedly"
            if exit_code is None
            else f"dockerd exited unexpectedly with code {exit_code}"
        )
        detail = output.render()
        instance.docker_daemon_error = f"{error}: {detail}" if detail else error
        self.instances.save_container_instance(instance)
