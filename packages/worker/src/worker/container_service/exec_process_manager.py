from __future__ import annotations

import shlex
from collections.abc import Iterable
from dataclasses import dataclass

from worker.container_service.models import (
    SandboxProcessEvent,
    SandboxProcessEventType,
    WorkerContainerServiceInstance,
    WorkerSandboxProcess,
)
from worker.container_service.protocols import (
    WorkerContainerRuntimeController,
    WorkerSandboxProcessManager,
)
from worker.sandbox_server import SandboxLogStream


@dataclass(slots=True)
class ContainerExecSandboxProcessManagerFactory:
    runtime: WorkerContainerRuntimeController

    def create_process_manager(
        self,
        instance: WorkerContainerServiceInstance,
    ) -> WorkerSandboxProcessManager:
        return ContainerExecSandboxProcessManager(instance=instance, runtime=self.runtime)

    def suspend_process_streams(self, instance: WorkerContainerServiceInstance) -> None:
        _ = instance

    def resume_process_streams(self, instance: WorkerContainerServiceInstance) -> None:
        _ = instance


@dataclass(slots=True)
class ContainerExecSandboxProcessManager:
    instance: WorkerContainerServiceInstance
    runtime: WorkerContainerRuntimeController

    def ready(self) -> bool:
        return True

    def ack(self, pid: int, seq: int, *, ok: bool) -> None:
        _ = pid, seq, ok

    def stream_exec(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: list[str],
    ) -> Iterable[SandboxProcessEvent]:
        pid = self.instance.sandbox_process_next_pid
        self.instance.sandbox_process_next_pid += 1
        self.instance.sandbox_processes[pid] = WorkerSandboxProcess(
            pid=pid,
            command=shlex.join(argv),
            cwd=cwd,
            env=list(env),
            exit_code=-1,
            running=True,
        )
        yield SandboxProcessEvent(event_type=SandboxProcessEventType.Started, pid=pid)

        try:
            response = self.runtime.exec_container(
                self.instance.container_id,
                argv=argv,
                env=env,
                cwd=cwd,
            )
        except Exception as exc:
            response_stdout = ""
            response_stderr = str(exc)
            exit_code = 1
        else:
            response_stdout = response.stdout
            response_stderr = response.stderr or (response.error_msg if not response.ok else "")
            exit_code = response.exit_code if response.ok else response.exit_code or 1

        if response_stdout:
            self.instance.sandbox_process_stdout[pid] = response_stdout
            yield SandboxProcessEvent(
                event_type=SandboxProcessEventType.Chunk,
                pid=pid,
                seq=1,
                stream=SandboxLogStream.Stdout,
                data=response_stdout.encode(),
            )
        if response_stderr:
            self.instance.sandbox_process_stderr[pid] = response_stderr
            yield SandboxProcessEvent(
                event_type=SandboxProcessEventType.Chunk,
                pid=pid,
                seq=2,
                stream=SandboxLogStream.Stderr,
                data=response_stderr.encode(),
            )

        process = self.instance.sandbox_processes[pid]
        process.exit_code = exit_code
        process.running = False
        yield SandboxProcessEvent(
            event_type=SandboxProcessEventType.Exited,
            pid=pid,
            exit_code=exit_code,
        )

    def status(self, pid: int) -> int | None:
        process = self.instance.sandbox_processes.get(pid)
        if process is None:
            return 1
        return None if process.running else process.exit_code

    def stdout(self, pid: int) -> str:
        return self.instance.sandbox_process_stdout.get(pid, "")

    def stderr(self, pid: int) -> str:
        return self.instance.sandbox_process_stderr.get(pid, "")

    def kill(self, pid: int) -> None:
        process = self.instance.sandbox_processes.get(pid)
        if process is None or not process.running:
            return
        process.exit_code = 143
        process.running = False

    def list_processes(self) -> list[WorkerSandboxProcess]:
        return list(self.instance.sandbox_processes.values())

    def cleanup(self) -> None:
        return None


__all__ = ["ContainerExecSandboxProcessManagerFactory"]
