from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from compute.agent_control import (
    ComputeAgentTokenState,
    RoutePrewarmAttemptPlan,
    plan_route_prewarm_attempt,
    plan_route_prewarm_result,
)
from networking.dialer import DEFAULT_BACKEND_ROUTE_DIAL_TIMEOUT_SECONDS, BackendRouteDialer
from pydantic import JsonValue
from shared.events import Event
from shared.routing import AgentBackendRoute


class RoutePrewarmEventAction(StrEnum):
    TransportPrewarm = "transport.prewarm"


class RoutePrewarmResourceType(StrEnum):
    AgentRoute = "agent-route"


class RoutePrewarmEventEmitter(Protocol):
    def emit(
        self,
        action: str,
        *,
        resource_type: str,
        resource_id: str,
        message: str,
        data: dict[str, JsonValue] | None = None,
        workspace_id: str | None = None,
    ) -> Event: ...


class RoutePrewarmRunner(Protocol):
    def submit(self, task: Callable[[], None]) -> None: ...


@runtime_checkable
class ClosableRoutePrewarmRunner(Protocol):
    def close(self) -> None: ...


def route_prewarm_shutdown_timeout_seconds(dial_timeout_seconds: float) -> float:
    return dial_timeout_seconds + 1.0


@dataclass(slots=True)
class ThreadRoutePrewarmRunner:
    thread_name_prefix: str = "route-prewarm"
    shutdown_timeout_seconds: float = route_prewarm_shutdown_timeout_seconds(
        DEFAULT_BACKEND_ROUTE_DIAL_TIMEOUT_SECONDS
    )
    _threads: set[threading.Thread] = field(default_factory=set, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def submit(self, task: Callable[[], None]) -> None:
        def run() -> None:
            try:
                task()
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())

        with self._lock:
            if self._closed:
                raise RuntimeError("route prewarm runner is closed")
            thread = threading.Thread(
                target=run,
                name=f"{self.thread_name_prefix}-{time.monotonic_ns()}",
                daemon=True,
            )
            self._threads.add(thread)
            thread.start()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            threads = tuple(self._threads)
        deadline = time.monotonic() + self.shutdown_timeout_seconds
        for thread in threads:
            thread.join(timeout=max(deadline - time.monotonic(), 0))
        with self._lock:
            active = tuple(thread.name for thread in self._threads if thread.is_alive())
        if active:
            raise RuntimeError(
                f"route prewarm shutdown timed out with {len(active)} active operation(s)"
            )


class RoutePrewarmService:
    def __init__(
        self,
        dialer: BackendRouteDialer,
        events: RoutePrewarmEventEmitter,
        *,
        runner: RoutePrewarmRunner | None = None,
        interval_seconds: float = 30.0,
    ) -> None:
        self.dialer = dialer
        self.events = events
        self.runner = runner or ThreadRoutePrewarmRunner(
            shutdown_timeout_seconds=route_prewarm_shutdown_timeout_seconds(
                dialer.config.timeout_seconds
            )
        )
        self.interval_seconds = interval_seconds
        self.attempts: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def prewarm_route(
        self,
        route: AgentBackendRoute,
        agent_state: ComputeAgentTokenState,
        *,
        now: datetime | None = None,
    ) -> RoutePrewarmAttemptPlan:
        with self._lock:
            attempt = plan_route_prewarm_attempt(
                route,
                self.attempts,
                now=now,
                interval_seconds=self.interval_seconds,
            )
            self.attempts = attempt.next_attempts
        if attempt.should_attempt:
            self.runner.submit(lambda: self.prewarm_route_once(route, agent_state))
        return attempt

    def prewarm_route_once(
        self,
        route: AgentBackendRoute,
        agent_state: ComputeAgentTokenState,
    ) -> None:
        started = time.monotonic()
        error = ""
        connection = None
        try:
            connection = self.dialer.dial_route(route)
        except Exception as exc:
            error = str(exc)
        finally:
            if connection is not None:
                connection.close()
        latency_ms = int(max(time.monotonic() - started, 0) * 1000)
        result = plan_route_prewarm_result(
            route,
            dial_latency_ms=latency_ms,
            error=error,
        )
        attrs: dict[str, JsonValue] = dict(result.attrs)
        event_data: dict[str, JsonValue] = {
            "workspace_id": agent_state.workspace_id,
            "pool": agent_state.pool,
            "machine_id": agent_state.machine_id,
            "worker_id": route.worker_id,
            "container_id": route.container_id,
            "route_id": route.route_id,
            "action": RoutePrewarmEventAction.TransportPrewarm.value,
            "status": result.status,
            "transport": str(route.transport),
            "message": result.message,
            "attrs": attrs,
        }
        self.events.emit(
            RoutePrewarmEventAction.TransportPrewarm.value,
            resource_type=RoutePrewarmResourceType.AgentRoute.value,
            resource_id=route.route_id,
            message=result.message or f"agent route {route.route_id} prewarm {result.status}",
            data=event_data,
            workspace_id=agent_state.workspace_id,
        )

    def close(self) -> None:
        runner = self.runner
        if isinstance(runner, ClosableRoutePrewarmRunner):
            runner.close()
