from __future__ import annotations

import signal
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType

import pytest
from runner.checkpoints import RestoredContainerIdentity
from runner.hooks import HookLogger, LifecycleContext
from shared.env import CHECKPOINT_ENABLED_ENV
from shared.lifecycle import LifecycleHookName, LifecycleHooks

from runner import serve

SignalHandler = signal.Handlers | Callable[[int, FrameType | None], None]


@dataclass(slots=True)
class _FakeProcess:
    exitcode: int | None = None
    alive: bool = True
    terminated: bool = False
    killed: bool = False
    joined: int = 0

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def kill(self) -> None:
        self.killed = True
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        del timeout
        self.joined += 1


def test_endpoint_process_manager_starts_capacity_and_stops_group_on_child_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = [_FakeProcess(), _FakeProcess(exitcode=17), _FakeProcess()]
    manager = serve.EndpointProcessManager(
        handler_ref="app:handler",
        stub_type="endpoint",
        host="127.0.0.1",
        port=8001,
        workers=3,
    )

    def start_worker(
        self: serve.EndpointProcessManager,
        index: int,
    ) -> _FakeProcess:
        del self
        return processes[index]

    monkeypatch.setattr(
        serve.EndpointProcessManager,
        "_start_worker",
        start_worker,
    )

    with pytest.raises(RuntimeError, match="endpoint worker process exited with 17"):
        manager.run()

    assert manager.processes == processes
    assert all(process.terminated for process in processes)
    assert all(process.joined == 1 for process in processes)


def test_function_endpoint_worker_joins_checkpoint_barrier_for_configured_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_calls: list[tuple[bool, int]] = []
    monkeypatch.setenv(CHECKPOINT_ENABLED_ENV, "true")

    def replace_signal(handled_signal: int, handler: SignalHandler) -> SignalHandler:
        del handled_signal
        return handler

    def load_handler(reference: str) -> Callable[[], None]:
        del reference
        return lambda: None

    def ignore_lifecycle_hooks(
        hooks: LifecycleHooks,
        hook: LifecycleHookName,
        context: LifecycleContext,
        *,
        log: HookLogger,
        capture_output: bool = True,
    ) -> None:
        del hooks, hook, context, log, capture_output

    def wait_for_checkpoint(
        *,
        enabled: bool,
        workers: int,
    ) -> RestoredContainerIdentity | None:
        checkpoint_calls.append((enabled, workers))
        return None

    def serve_once(self: serve.EndpointServeRunner) -> None:
        self.run_startup_hooks()

    monkeypatch.setattr(signal, "signal", replace_signal)
    monkeypatch.setattr(serve, "load_callable", load_handler)
    monkeypatch.setattr(serve, "run_lifecycle_hooks", ignore_lifecycle_hooks)
    monkeypatch.setattr(
        serve,
        "wait_for_checkpoint",
        wait_for_checkpoint,
    )
    monkeypatch.setattr(serve.EndpointServeRunner, "serve_forever", serve_once)

    serve._run_function_endpoint_worker(
        "app:handler",
        "endpoint",
        "127.0.0.1",
        8001,
        4,
    )

    assert checkpoint_calls == [(True, 4)]
