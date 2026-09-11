from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from shared.enums import StringEnum
from shared.signals import (
    DEFAULT_SIGNAL_SET_TTL_SECONDS,
    SignalClearRequest,
    SignalClearResponse,
    SignalMonitorRequest,
    SignalMonitorResponse,
    SignalMonitorSnapshot,
    SignalSetRequest,
    SignalSetResponse,
)
from typing_extensions import Self

from lazycloud.control import resolve_control_client_config
from lazycloud.env import called_on_import


class SignalAction(StringEnum):
    Listen = "listen"
    Set = "set"
    Clear = "clear"
    Monitor = "monitor"


SignalHandler = Callable[..., Any]


class SignalService(Protocol):
    def signal_set(self, request: SignalSetRequest) -> SignalSetResponse: ...

    def signal_clear(self, request: SignalClearRequest) -> SignalClearResponse: ...

    def signal_monitor_once(
        self,
        request: SignalMonitorRequest,
        *,
        has_handler: bool = False,
        clear_after_interval_seconds: int | None = None,
    ) -> SignalMonitorSnapshot: ...


@dataclass
class Signal:
    name: str
    handler: SignalHandler | None = None
    clear_after_interval: int | None = -1
    workspace_name: str | None = None
    sleeper: Callable[[float], None] = time.sleep
    client: SignalService | None = field(default=None, init=False, repr=False)
    signal_service: SignalService | None = field(default=None, init=False, repr=False)
    endpoint: str | None = field(default=None, init=False, repr=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout_seconds: float = field(default=10.0, init=False, repr=False)
    _monitor_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _monitor_stop: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.handler is not None and called_on_import():
            self.start_monitoring()

    def listen(self, handler: SignalHandler) -> SignalHandler:
        self.handler = handler
        return handler

    def set(self, ttl: int | None = DEFAULT_SIGNAL_SET_TTL_SECONDS) -> bool:
        self._signal_service().signal_set(
            SignalSetRequest(
                workspace_name=self._workspace_name(),
                name=self.name,
                ttl_seconds=DEFAULT_SIGNAL_SET_TTL_SECONDS if ttl is None else ttl,
            )
        )
        return True

    def clear(self) -> bool:
        self._signal_service().signal_clear(
            SignalClearRequest(workspace_name=self._workspace_name(), name=self.name)
        )
        return True

    def monitor_once(self) -> SignalMonitorResponse:
        snapshot = self._signal_service().signal_monitor_once(
            SignalMonitorRequest(workspace_name=self._workspace_name(), name=self.name),
            has_handler=self.handler is not None,
            clear_after_interval_seconds=self.clear_after_interval,
        )
        self._handle_monitor_snapshot(snapshot)
        return snapshot.response

    def start_monitoring(self, *, poll_interval_seconds: float = 1.0) -> threading.Thread:
        if self.handler is None:
            msg = "signal handler is required before monitoring"
            raise RuntimeError(msg)
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return self._monitor_thread
        self._monitor_stop = threading.Event()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            kwargs={"poll_interval_seconds": poll_interval_seconds},
            daemon=True,
        )
        self._monitor_thread.start()
        return self._monitor_thread

    def stop_monitoring(self, *, timeout_seconds: float | None = None) -> None:
        self._monitor_stop.set()
        thread = self._monitor_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout_seconds)

    def _monitor_loop(self, *, poll_interval_seconds: float) -> None:
        service = self._signal_service()
        while not self._monitor_stop.is_set():
            snapshot = service.signal_monitor_once(
                SignalMonitorRequest(workspace_name=self._workspace_name(), name=self.name),
                has_handler=self.handler is not None,
                clear_after_interval_seconds=self.clear_after_interval,
            )
            self._handle_monitor_snapshot(snapshot)
            if self._monitor_stop.is_set():
                break
            self.sleeper(poll_interval_seconds)

    def _handle_monitor_snapshot(self, snapshot: SignalMonitorSnapshot) -> None:
        if snapshot.should_call_handler and self.handler is not None:
            self.handler()
        if snapshot.should_clear_after_handler:
            self.sleeper(snapshot.clear_after_seconds)
            self.clear()

    def _bind_control(
        self,
        client: SignalService | None = None,
        *,
        signal_service: SignalService | None = None,
        workspace_name: str | None = None,
        endpoint: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Self:
        self.client = client
        self.signal_service = signal_service
        if workspace_name is not None:
            self.workspace_name = workspace_name
        if endpoint is not None:
            self.endpoint = endpoint
        if token is not None:
            self.token = token
        if timeout_seconds is not None:
            self.timeout_seconds = timeout_seconds
        return self

    def _signal_service(self) -> SignalService:
        if self.signal_service is not None:
            return self.signal_service
        if self.client is None:
            from lazycloud.clients.signal.control import SignalControlClient

            config = resolve_control_client_config(
                workspace=self.workspace_name,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            self.client = SignalControlClient.from_endpoint(
                config.endpoint,
                token=config.token,
                timeout_seconds=config.timeout_seconds,
                workspace=config.workspace,
            )
        return self.client

    def _workspace_name(self) -> str:
        return resolve_control_client_config(
            workspace=self.workspace_name,
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.timeout_seconds,
        ).workspace
