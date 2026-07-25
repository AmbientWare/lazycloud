from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from threading import Event, Thread

from worker.container_service.models import (
    SandboxDockerDaemonStatus,
    SandboxProcessEvent,
    SandboxProcessEventType,
    WorkerContainerServiceInstance,
    WorkerSandboxProcess,
)
from worker.container_service.state import LocalWorkerContainerInstanceStore
from worker.sandbox_docker import (
    DOCKER_DAEMON_COMMAND,
    DOCKER_SANDBOX_ENV,
    WorkerSandboxDockerService,
)
from worker.sandbox_server import SandboxLogStream


@dataclass(slots=True)
class ProcessManager:
    events: Iterable[SandboxProcessEvent]
    commands: list[list[str]] = field(default_factory=list)
    environments: list[list[str]] = field(default_factory=list)
    acknowledgements: list[tuple[int, int, bool]] = field(default_factory=list)
    entered: Event | None = None
    release: Event | None = None

    def ready(self) -> bool:
        return True

    def stream_exec(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: list[str],
    ) -> Iterable[SandboxProcessEvent]:
        _ = cwd, env
        self.commands.append(argv)
        self.environments.append(env)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=2)
        return self.events

    def ack(self, pid: int, seq: int, *, ok: bool) -> None:
        self.acknowledgements.append((pid, seq, ok))

    def status(self, pid: int) -> int | None:
        _ = pid
        return None

    def stdout(self, pid: int) -> str:
        _ = pid
        return ""

    def stderr(self, pid: int) -> str:
        _ = pid
        return ""

    def kill(self, pid: int) -> None:
        _ = pid

    def list_processes(self) -> list[WorkerSandboxProcess]:
        return []

    def cleanup(self) -> None:
        return


@dataclass(slots=True)
class ProcessManagerFactory:
    managers: list[ProcessManager]

    def create_process_manager(self, instance: WorkerContainerServiceInstance) -> ProcessManager:
        _ = instance
        return self.managers.pop(0)

    def suspend_process_streams(self, instance: WorkerContainerServiceInstance) -> None:
        _ = instance

    def resume_process_streams(self, instance: WorkerContainerServiceInstance) -> None:
        _ = instance


def _completed_process() -> list[SandboxProcessEvent]:
    return [
        SandboxProcessEvent(event_type=SandboxProcessEventType.Started, pid=10),
        SandboxProcessEvent(event_type=SandboxProcessEventType.Exited, pid=10, exit_code=0),
    ]


def _daemon_events(stopped: Event) -> Iterable[SandboxProcessEvent]:
    yield SandboxProcessEvent(event_type=SandboxProcessEventType.Started, pid=77)
    stopped.wait(timeout=2)
    yield SandboxProcessEvent(event_type=SandboxProcessEventType.Exited, pid=77, exit_code=0)


def _reset_daemon_stream() -> Iterable[SandboxProcessEvent]:
    raise ConnectionResetError("supervisor stopped")
    yield


def _failed_daemon_stream() -> Iterable[SandboxProcessEvent]:
    yield SandboxProcessEvent(
        event_type=SandboxProcessEventType.Chunk,
        pid=77,
        seq=1,
        stream=SandboxLogStream.Stderr,
        data="daemon Δ diagnostic".encode(),
    )
    raise ConnectionResetError("supervisor stopped")


def test_docker_enabled_sandbox_starts_probes_and_stops_daemon() -> None:
    store = LocalWorkerContainerInstanceStore()
    instance = WorkerContainerServiceInstance(
        container_id="sandbox-1",
        root_path="/rootfs",
        docker_enabled=True,
        sandbox_process_manager_ready=True,
        docker_daemon_status=SandboxDockerDaemonStatus.Stopped,
    )
    store.save_container_instance(instance)
    stopped = Event()
    setup = ProcessManager(_completed_process())
    forwarding = ProcessManager(_completed_process())
    daemon = ProcessManager(_daemon_events(stopped))
    probe = ProcessManager(_completed_process())
    shutdown = ProcessManager(_completed_process())
    service = WorkerSandboxDockerService(
        instances=store,
        process_managers=ProcessManagerFactory([setup, forwarding, daemon, probe, shutdown]),
        startup_timeout_seconds=1,
        ready_poll_seconds=0.001,
    )

    service.ensure_ready(instance)

    ready = store.get_container_instance("sandbox-1")
    assert ready is not None
    assert ready.docker_daemon_status is SandboxDockerDaemonStatus.Ready
    assert ready.docker_daemon_pid == 77
    assert daemon.commands == [DOCKER_DAEMON_COMMAND]
    assert "--userland-proxy=false" in daemon.commands[0]
    assert daemon.environments == [DOCKER_SANDBOX_ENV]
    assert probe.commands == [["docker", "info"]]
    assert probe.environments == [DOCKER_SANDBOX_ENV]

    service.stop("sandbox-1")
    stopped.set()

    terminated = store.get_container_instance("sandbox-1")
    assert terminated is not None
    assert terminated.docker_daemon_status is SandboxDockerDaemonStatus.Stopped
    assert terminated.docker_daemon_pid == 0


def test_concurrent_readiness_serializes_one_daemon_start() -> None:
    store = LocalWorkerContainerInstanceStore()
    instance = WorkerContainerServiceInstance(
        container_id="sandbox-1",
        root_path="/rootfs",
        docker_enabled=True,
        sandbox_process_manager_ready=True,
        docker_daemon_status=SandboxDockerDaemonStatus.Stopped,
    )
    store.save_container_instance(instance)
    first_entered = Event()
    release_first = Event()
    daemon_stopped = Event()
    setup = ProcessManager(
        _completed_process(),
        entered=first_entered,
        release=release_first,
    )
    forwarding = ProcessManager(_completed_process())
    daemon = ProcessManager(_daemon_events(daemon_stopped))
    first_probe = ProcessManager(_completed_process())
    second_probe = ProcessManager(_completed_process())
    factory = ProcessManagerFactory([setup, forwarding, daemon, first_probe, second_probe])
    service = WorkerSandboxDockerService(
        instances=store,
        process_managers=factory,
        startup_timeout_seconds=1,
        ready_poll_seconds=0.001,
    )
    errors: list[BaseException] = []

    def ensure_ready() -> None:
        try:
            service.ensure_ready(instance)
        except BaseException as exc:
            errors.append(exc)

    first = Thread(target=ensure_ready)
    second = Thread(target=ensure_ready)
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    daemon_stopped.set()

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert daemon.commands == [DOCKER_DAEMON_COMMAND]
    assert first_probe.commands == [["docker", "info"]]
    assert second_probe.commands == [["docker", "info"]]
    assert factory.managers == []


def test_docker_daemon_stream_disconnect_is_quiet_during_expected_stop() -> None:
    store = LocalWorkerContainerInstanceStore()
    instance = WorkerContainerServiceInstance(
        container_id="sandbox-1",
        root_path="/rootfs",
        docker_enabled=True,
        docker_daemon_status=SandboxDockerDaemonStatus.Stopping,
    )
    store.save_container_instance(instance)
    service = WorkerSandboxDockerService(
        instances=store,
        process_managers=ProcessManagerFactory([]),
    )

    manager = ProcessManager([])
    service._consume_daemon(
        "sandbox-1",
        manager,
        [
            SandboxProcessEvent(
                event_type=SandboxProcessEventType.Chunk,
                pid=77,
                seq=1,
                stream=SandboxLogStream.Stderr,
                data=b"graceful shutdown",
            ),
            SandboxProcessEvent(
                event_type=SandboxProcessEventType.Exited,
                pid=77,
                exit_code=0,
            ),
        ],
    )

    stopped = store.get_container_instance("sandbox-1")
    assert stopped is not None
    assert stopped.docker_daemon_status is SandboxDockerDaemonStatus.Stopping
    assert stopped.docker_daemon_error == ""
    assert manager.acknowledgements == [(77, 1, True)]


def test_unexpected_docker_daemon_stream_disconnect_records_failure() -> None:
    store = LocalWorkerContainerInstanceStore()
    instance = WorkerContainerServiceInstance(
        container_id="sandbox-1",
        root_path="/rootfs",
        docker_enabled=True,
        docker_daemon_status=SandboxDockerDaemonStatus.Ready,
    )
    store.save_container_instance(instance)
    service = WorkerSandboxDockerService(
        instances=store,
        process_managers=ProcessManagerFactory([]),
    )

    manager = ProcessManager([])
    service._consume_daemon(
        "sandbox-1",
        manager,
        _failed_daemon_stream(),
    )

    failed = store.get_container_instance("sandbox-1")
    assert failed is not None
    assert failed.docker_daemon_status is SandboxDockerDaemonStatus.Failed
    assert failed.docker_daemon_error == (
        "ConnectionResetError: supervisor stopped: stderr: daemon Δ diagnostic"
    )
    assert manager.acknowledgements == [(77, 1, True)]


def test_unexpected_docker_daemon_exit_records_bounded_utf8_output() -> None:
    store = LocalWorkerContainerInstanceStore()
    instance = WorkerContainerServiceInstance(
        container_id="sandbox-1",
        root_path="/rootfs",
        docker_enabled=True,
        docker_daemon_status=SandboxDockerDaemonStatus.Starting,
    )
    store.save_container_instance(instance)
    manager = ProcessManager([])
    service = WorkerSandboxDockerService(
        instances=store,
        process_managers=ProcessManagerFactory([]),
    )
    stderr = (b"old-output\n" * 1_000) + "fatal: déjà vu".encode()

    service._consume_daemon(
        "sandbox-1",
        manager,
        [
            SandboxProcessEvent(
                event_type=SandboxProcessEventType.Chunk,
                pid=77,
                seq=1,
                stream=SandboxLogStream.Stdout,
                data=b"daemon booting\n",
            ),
            SandboxProcessEvent(
                event_type=SandboxProcessEventType.Chunk,
                pid=77,
                seq=2,
                stream=SandboxLogStream.Stderr,
                data=stderr,
            ),
            SandboxProcessEvent(
                event_type=SandboxProcessEventType.Exited,
                pid=77,
                exit_code=1,
            ),
        ],
    )

    failed = store.get_container_instance("sandbox-1")
    assert failed is not None
    assert failed.docker_daemon_status is SandboxDockerDaemonStatus.Failed
    assert failed.docker_daemon_error.startswith("dockerd exited unexpectedly with code 1: ")
    assert "stdout: daemon booting" in failed.docker_daemon_error
    assert "stderr: [truncated]" in failed.docker_daemon_error
    assert "fatal: déjà vu" in failed.docker_daemon_error
    assert "old-output" in failed.docker_daemon_error
    assert len(failed.docker_daemon_error.encode()) < 8_500
    assert manager.acknowledgements == [(77, 1, True), (77, 2, True)]
