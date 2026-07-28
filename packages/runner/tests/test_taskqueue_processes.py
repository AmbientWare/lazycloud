from __future__ import annotations

from collections.abc import Callable

import pytest
import runner.taskqueue as taskqueue_module
from runner.checkpoints import RestoredContainerIdentity
from runner.taskqueue import (
    TaskQueueProcessManager,
    TaskQueueRunner,
    TaskQueueRunnerConfig,
    config_from_env,
)
from shared.env import TASK_QUEUE_WORKERS_ENV


class _WorkerProcess:
    def __init__(
        self,
        *,
        target: Callable[[TaskQueueRunnerConfig], None],
        args: tuple[TaskQueueRunnerConfig],
        name: str,
    ) -> None:
        self.target = target
        self.args = args
        self.name = name
        self.exitcode: int | None = 7 if name.endswith("-1") else None
        self.started = False
        self.terminated = False
        self.killed = False
        self.join_timeouts: list[float | None] = []

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return self.exitcode is None

    def terminate(self) -> None:
        self.terminated = True
        self.exitcode = -15

    def kill(self) -> None:
        self.killed = True
        self.exitcode = -9

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)


def test_task_queue_runner_waits_for_all_workers_before_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bool, int]] = []

    def load_handler(reference: str) -> Callable[[], None]:
        del reference
        return lambda: None

    monkeypatch.setattr(taskqueue_module, "load_callable", load_handler)

    def wait_for_checkpoint(
        *,
        enabled: bool,
        workers: int,
    ) -> RestoredContainerIdentity:
        calls.append((enabled, workers))
        return RestoredContainerIdentity(
            container_id="restored-container",
            container_hostname="restored-host",
        )

    monkeypatch.setattr(taskqueue_module, "wait_for_checkpoint", wait_for_checkpoint)
    runner = TaskQueueRunner(
        TaskQueueRunnerConfig(
            stub_id="stub-1",
            handler_ref="pkg.queue:handler",
            workers=3,
            checkpoint_enabled=True,
            container_id="original-container",
            container_hostname="original-host",
        )
    )

    runner.run_startup_hooks_once()

    assert calls == [(True, 3)]
    assert runner.config.container_id == "restored-container"
    assert runner.config.container_hostname == "restored-host"


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_task_queue_runner_config_rejects_invalid_worker_capacity(value: str) -> None:
    with pytest.raises(RuntimeError, match=TASK_QUEUE_WORKERS_ENV):
        config_from_env(
            {
                "STUB_ID": "stub-1",
                "HANDLER": "pkg.queue:handler",
                TASK_QUEUE_WORKERS_ENV: value,
            }
        )


def test_task_queue_process_manager_starts_capacity_and_stops_siblings_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[_WorkerProcess] = []

    def process_factory(
        *,
        target: Callable[[TaskQueueRunnerConfig], None],
        args: tuple[TaskQueueRunnerConfig],
        name: str,
    ) -> _WorkerProcess:
        process = _WorkerProcess(target=target, args=args, name=name)
        processes.append(process)
        return process

    monkeypatch.setattr(taskqueue_module, "Process", process_factory)
    manager = TaskQueueProcessManager(
        TaskQueueRunnerConfig(
            stub_id="stub-1",
            handler_ref="pkg.queue:handler",
            workers=3,
        ),
        poll_interval_seconds=0,
    )

    with pytest.raises(RuntimeError, match="exited with 7"):
        manager.run()

    assert [process.name for process in processes] == [
        "taskqueue-worker-0",
        "taskqueue-worker-1",
        "taskqueue-worker-2",
    ]
    assert all(process.started for process in processes)
    assert processes[0].terminated
    assert not processes[1].terminated
    assert processes[2].terminated
    assert all(len(process.join_timeouts) == 1 for process in processes)
    assert all(
        timeout is not None and 0 <= timeout <= 5
        for process in processes
        for timeout in process.join_timeouts
    )
