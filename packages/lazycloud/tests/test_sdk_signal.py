from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest
from lazycloud.abstractions.experimental.signal import Signal
from shared.env import IMPORTING_USER_CODE_ENV, WORKSPACE_NAME_ENV
from shared.signals import (
    SignalClearRequest,
    SignalClearResponse,
    SignalMonitorRequest,
    SignalMonitorResponse,
    SignalMonitorSnapshot,
    SignalSetRequest,
    SignalSetResponse,
)


@dataclass
class FakeSignalService:
    set_count: int = 0
    clear_count: int = 0
    monitor_count: int = 0
    set_response: bool = True
    monitor_set: bool = True
    monitor_events: list[bool] = field(default_factory=list)

    def signal_set(self, request: SignalSetRequest) -> SignalSetResponse:
        assert request.workspace_name == "workspace"
        assert request.name == "reload"
        self.set_count += 1
        if not self.set_response:
            raise RuntimeError("signal set failed")
        return SignalSetResponse()

    def signal_clear(self, request: SignalClearRequest) -> SignalClearResponse:
        assert request.workspace_name == "workspace"
        assert request.name == "reload"
        self.clear_count += 1
        return SignalClearResponse()

    def signal_monitor_once(
        self,
        request: SignalMonitorRequest,
        *,
        has_handler: bool = False,
        clear_after_interval_seconds: int | None = None,
    ) -> SignalMonitorSnapshot:
        assert request.workspace_name == "workspace"
        assert has_handler is True
        self.monitor_count += 1
        clear_after_seconds = max(clear_after_interval_seconds or 0, 0)
        return SignalMonitorSnapshot(
            ok=True,
            set=self.monitor_set,
            should_call_handler=self.monitor_set and has_handler,
            should_clear_after_handler=self.monitor_set and clear_after_seconds > 0,
            clear_after_seconds=clear_after_seconds,
            response=SignalMonitorResponse(set=self.monitor_set),
        )

    def monitor(
        self,
        name: str,
        *,
        poll_interval_seconds: float = 1.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> Iterator[SignalMonitorResponse]:
        _ = (poll_interval_seconds, sleeper)
        assert name == "reload"
        self.monitor_count += 1
        for event in self.monitor_events:
            yield SignalMonitorResponse(set=event)


def test_signal_uses_active_workspace_for_set_clear_and_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(WORKSPACE_NAME_ENV, "workspace")
    service = FakeSignalService()
    calls: list[str] = []
    sleeps: list[float] = []
    signal = Signal(
        "reload",
        clear_after_interval=2,
        sleeper=sleeps.append,
    )._bind_control(signal_service=service)

    @signal.listen
    def on_signal() -> None:
        calls.append("called")

    assert signal.set(ttl=30) is True
    assert signal.clear() is True
    response = signal.monitor_once()

    assert response.set is True
    assert calls == ["called"]
    assert sleeps == [2]
    assert service.set_count == 1
    assert service.clear_count == 2


def test_signal_monitor_without_handler_does_not_start() -> None:
    signal = Signal("reload")._bind_control(signal_service=FakeSignalService())

    try:
        signal.start_monitoring()
    except RuntimeError as exc:
        assert "handler is required" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("start_monitoring should fail without a handler")


def test_signal_auto_starts_stream_monitor_during_user_code_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeSignalService(monitor_events=[True])
    calls: list[str] = []
    sleeps: list[float] = []

    signal = Signal(
        "reload",
        workspace_name="workspace",
        clear_after_interval=3,
        sleeper=sleeps.append,
    )._bind_control(signal_service=service)

    @signal.listen
    def on_signal() -> None:
        calls.append("called")
        signal.stop_monitoring()

    monkeypatch.setenv(IMPORTING_USER_CODE_ENV, "true")
    signal.__post_init__()

    assert signal._monitor_thread is not None
    signal._monitor_thread.join(timeout=1)

    assert calls == ["called"]
    assert sleeps == [3]
    assert service.monitor_count == 1
    assert service.clear_count == 1
