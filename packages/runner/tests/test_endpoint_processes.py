from __future__ import annotations

from dataclasses import dataclass

import pytest

from runner import serve


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
